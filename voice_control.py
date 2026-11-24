"""
Controle central da voz da Lia.

Por que existe: com tarefas em segundo plano, várias partes do programa
passam a ter algo a dizer (resultado de uma tarefa, pedido de confirmação,
no futuro um alarme ou um aviso de reunião). Se cada uma chamasse
voz.falar() por conta própria, as falas se sobreporiam e a Lia poderia
falar por cima do usuário. Aqui existe UM dono da voz:

  - Só a thread principal (a do loop da Lia) fala de verdade. Quem chamar
    falar() de outra thread NÃO fala: a frase vai pra fila de pendentes.
    Isso é estrutural: não depende de ninguém lembrar de pedir licença.
  - Quem tem algo a dizer chama anunciar(): a mensagem entra na fila
    (por prioridade) e espera o momento certo.
  - O loop da Lia entrega a fila só em momentos seguros (entregar()):
      * em espera pela wake word, depois de um período de silêncio;
      * no fim de um turno (depois que ela respondeu), antes de reabrir o
        microfone, e só se o usuário não estiver falando já.
    Nunca no meio da fala do usuário, nunca no meio de uma resposta dela,
    nunca durante uma confirmação em andamento.

O ControleDeVoz também faz de PROXY da voz (falar, aguardar_fim, parar e
qualquer atributo, como suporta_sincronia): o resto do código continua
chamando voz.falar() como sempre, sem saber que existe um controle.
"""

import heapq
import itertools
import math
import threading
import time
from array import array
from dataclasses import dataclass, field

import config
import microphone
from events import emit as _emit_barramento

PRIORIDADE_CONFIRMATION = 0   # blocks a task waiting for a response
PRIORIDADE_RESULT = 1     # task finished
PRIORIDADE_WARNING = 2         # everything else (alarms, future reminders...)

# Tunable values from config.py; these are used if a setting is absent.
SILENCE_MINIMUM = getattr(config,"NOTIF_SILENCIO_SEGUNDOS",1.0)
FATOR_VOICE = getattr(config,"NOTIF_FATOR_VOZ",2.5)
THRESHOLD_MINIMUM = getattr(config,"NOTIF_LIMIAR_MINIMO",800.0)
WINDOW_ESCUTA = 0.5          # fresh listening window before speaking at turn end
WINDOW_ENTRE_MESSAGES = 0.3


@dataclass
class Message:
    text: str = ""
    origin: str = "sistema"
    type: str = "notice"                  # warning | result | confirmation
    prioridade: int = PRIORIDADE_WARNING
    task_id: int = None
    title: str = None                   # title of the card in the interface
    criada: float = field(default_factory=time.time)
    ao_deliver: object = None           # callback(msg) after it is spoken
    execute: object = None              # callback(mic_stream): interativa
    valid: object = None                # callback() -> False = descartar


def _rms(data):
    """Volume (RMS) de um bloco de áudio PCM 16 bits (bytes ou array)."""

    if isinstance(data,(bytes,bytearray,memoryview)):

        amostras = array("h")
        amostras.frombytes(bytes(data)[: len(data) // 2 * 2])

        if not amostras:
            return 0.0

        return math.sqrt(sum(x * x for x in amostras) / len(amostras))

    import numpy as np

    return float(np.sqrt(np.mean(np.asarray(data,dtype=np.float64) ** 2)))


class VoiceController:

    def __init__(self,voice,emit=None):

        self._voice = voice
        self._emit = emit if emit is not None else _emit_barramento

        # Only this thread speaks aloud (created at startup by the main thread).
        self._owner_thread = threading.current_thread()

        self._queue = []
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._entregando = False

        self._last_sound = 0.0
        self.state = "ocioso"   # informational: idle | turn | conversation

    # ------------------------------------------------------------------
    # voice proxy
    # ------------------------------------------------------------------

    def _is_owner_thread(self):
        return threading.current_thread() is self._owner_thread

    def speak(self,text,**kwargs):

        if not self._is_owner_thread():

            # Another thread tried to speak; queue it instead.
            print(
                "[ℹ] Fala pedida fora da thread principal: "
                "indo pra fila de pendentes."
            )

            self.announce(text,origin="thread")

            return

        self._voice.speak(text,**kwargs)

    def wait_end(self,*args,**kwargs):

        if not self._is_owner_thread():
            return

        self._voice.wait_end(*args,**kwargs)

    def stop(self):
        self._voice.stop()

    def __getattr__(self,name):

        # supports_sync, rate, name, and other attributes come from the real voice.
        return getattr(self._voice,name)

    # ------------------------------------------------------------------
    # pending queue
    # ------------------------------------------------------------------

    def announce(
        self,
        text="",
        origin="sistema",
        type="notice",
        prioridade=None,
        task_id=None,
        ao_deliver=None,
        execute=None,
        valid=None,
        title=None,
    ):
        """Pode ser chamado de QUALQUER thread. Nunca fala na hora."""

        if prioridade is None:
            prioridade = PRIORIDADE_WARNING

        message = Message(
            text=text,
            origin=origin,
            type=type,
            prioridade=prioridade,
            task_id=task_id,
            ao_deliver=ao_deliver,
            execute=execute,
            valid=valid,
            title=title,
        )

        with self._lock:
            heapq.heappush(
                self._queue,
                (message.prioridade,next(self._seq),message)
            )

        return message

    def pending(self):

        with self._lock:
            return len(self._queue)

    def _remove(self,may_interact):
        """Próxima mensagem; interativas só saem se houver microphone."""

        with self._lock:

            adiadas = []
            escolhida = None

            while self._queue:

                item = heapq.heappop(self._queue)
                message = item[2]

                if message.execute and not may_interact:
                    adiadas.append(item)
                    continue

                escolhida = message
                break

            for item in adiadas:
                heapq.heappush(self._queue,item)

        return escolhida

    # ------------------------------------------------------------------
    # sensor for "is the user speaking?"
    # ------------------------------------------------------------------

    def _threshold(self):

        noise = getattr(microphone,"NIVEL_RUIDO_AMBIENTE",0) or 0

        return max(THRESHOLD_MINIMUM,noise * FATOR_VOICE)

    def sense(self,data):
        """
        O loop chama a cada bloco de microfone em espera: se tem som
        acima do ruído, alguém está falando (talvez com a Lia).
        """

        try:

            if _rms(data) > self._threshold():
                self._last_sound = time.monotonic()

        except Exception:
            pass

    def is_silent(self):
        return time.monotonic() - self._last_sound

    def _free(self,mic_stream,window):
        """Escuta de verdade por `janela` s: False se alguém estiver falando."""

        try:

            discard = getattr(microphone,"descartar_buffer",None)

            if discard:
                discard(mic_stream,extra=0.2)

            blocks = max(1,int(
                window * config.RATE / config.CHUNK
            ))

            threshold = self._threshold()

            for _ in range(blocks):

                data = mic_stream.read(
                    config.CHUNK,
                    exception_on_overflow=False
                )

                if _rms(data) > threshold:
                    self._last_sound = time.monotonic()
                    return False

            return True

        except Exception as e:

            print(f"[!] Não consegui checar o silêncio: {e}")

            return True

    # ------------------------------------------------------------------
    # delivery (main thread only, at safe points)
    # ------------------------------------------------------------------

    def should_deliver(self):
        """Em espera pela wake word: há pendentes e já faz silêncio?"""

        return (
            self.pending() > 0
            and not self._entregando
            and self.is_silent() >= SILENCE_MINIMUM
        )

    def deliver(
        self,
        mic_stream=None,
        oww_model=None,
        context="ocioso",
        state_final=None,
    ):
        """
        Fala as mensagens pendentes, uma de cada vez. Devolve quantas
        entregou. Nunca levanta exceção.

        context "ocioso": o loop já vinha escutando (deve_entregar()).
        context "fim_de_turno": a Lia acabou de falar e o microfone
        estava fechado, então escuta de verdade antes: se o usuário já
        estiver falando, não entrega (o loop vai ouvi-lo).
        """

        if not self._is_owner_thread() or self._entregando:
            return 0

        if self.pending() == 0:
            return 0

        entregues = 0
        self._entregando = True

        try:

            if context != "ocioso" and mic_stream is not None:

                if not self._free(mic_stream,WINDOW_ESCUTA):
                    return 0

            while True:

                message = self._remove(may_interact=mic_stream is not None)

                if message is None:
                    break

                if message.valid and not self._call(message.valid,True):
                    continue

                try:
                    self._speak_message(message,mic_stream)
                except Exception as e:
                    print(f"[!] Erro ao entregar mensagem: {e}")

                entregues += 1

                if message.ao_deliver:
                    self._call(message.ao_deliver,None,message)

                # The user may have started speaking while Lia was talking:
                # the others wait for the next opportunity.
                if (
                    self.pending() > 0
                    and mic_stream is not None
                    and not self._free(mic_stream,WINDOW_ENTRE_MESSAGES)
                ):
                    break

        except Exception as e:

            print(f"[!] Erro na entrega de pendentes: {e}")

        finally:

            self._entregando = False

            if entregues:

                # The microphone picked up Lia’s own voice: discard it and require
                # silence before the next delivery.
                if mic_stream is not None:

                    discard = getattr(microphone,"descartar_buffer",None)

                    if discard:
                        try:
                            discard(mic_stream,extra=0.3)
                        except Exception:
                            pass

                if oww_model is not None:
                    try:
                        oww_model.reset()
                    except Exception:
                        pass

                self._last_sound = time.monotonic()

                if state_final:
                    self._emit("state",state=state_final)

        return entregues

    def _speak_message(self,message,mic_stream):

        if message.execute:

            # Interactive (confirmation): Lia speaks and listens herself.
            message.execute(mic_stream)

            return

        print(f"\n[📣 {message.origin}] {message.text}")

        # The card uses the user’s latest speech as its title: a spontaneous
        # warning must not appear beneath an older question.
        self._emit("user_speech",text=message.title or "Lia")

        self._emit("state",state="speaking")
        self._emit("response_end",text=message.text)

        self._voice.speak(message.text)
        self._voice.wait_end()

        # Lia finished announcing a task result: the panel removes its
        # graph now (only after the speech, never before).
        if message.type == "result" and message.task_id is not None:
            self._emit("task",id=message.task_id,state="spoken")

    @staticmethod
    def _call(function,default,*args):

        try:
            return function(*args)
        except Exception as e:
            print(f"[!] Erro em callback da fila de voz: {e}")
            return default