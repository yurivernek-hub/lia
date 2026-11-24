"""
Voz do Jarvis (Piper).

Dois jeitos de falar, escolhidos sozinhos:

1) PiperEmProcesso (preferido): usa a biblioteca piper-tts dentro do
   próprio Jarvis. Assim ele SABE exatamente quando cada frase terminou
   de ser sintetizada e quanto áudio tocou — "terminou de falar" é
   preciso, mesmo quando a voz demora pra gerar a frase seguinte.

2) PiperCLI (reserva): o executável `piper` por pipe. Só adivinha o fim
   pela ociosidade do áudio, então pode errar em vozes lentas.

Sincronia com a tela: falar(texto, on_start=f) chama f() no instant em
que o PRIMEIRO som daquele texto sai no alto-falante (a fila toca os
textos em sequência, então cada bloco dispara no seu momento). Só o
PiperEmProcesso sabe fazer isso (suporta_sincronia = True); o PiperCLI
chama f() na hora, e quem usa deve falar um bloco por vez (ver
morning_routine.py).
"""

import array
import heapq
import json
import math
import os
import queue
import subprocess
import threading
import time

from config import PIPER_EXEC,PIPER_MODEL
from events import bus,emit
from utils import clear_text_for_voice

# Latency from aplay until sound actually comes out of the speaker.
MARGEM_ALSA = 0.3

# Speech volume (for interface reactivity): analysis window, delay before the
# sound reaches the speaker, and RMS value counted as "maximum volume".
WINDOW_LEVEL = 0.04
DELAY_VISUAL = 0.12
LEVEL_REFERENCE = 7000.0

# If there is no progress (no new audio and no completed phrase) for this
# long, stop waiting (this only guards against a stall).
WITHOUT_PROGRESS_MAX = 90.0


def _model_rate():
    """Taxa de amostragem da voz (lida do .onnx.json; padrão 22050)."""

    try:

        with open(PIPER_MODEL + ".json",encoding="utf-8") as f:
            return int(json.load(f)["audio"]["sample_rate"])

    except Exception:

        return 22050


def _open_aplay(rate):

    return subprocess.Popen(
        ["aplay","-r",str(rate),"-f","S16_LE","-c","1","-q"],
        stdin=subprocess.PIPE,
        stderr=subprocess.DEVNULL
    )


def _close(proc):

    if not proc:
        return

    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass

    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _execute_action(action):
    """Roda um callback de sincronia sem nunca derrubar a voz."""

    try:
        action()
    except Exception as e:
        print(f"[!] Erro no callback da fala: {e}")


class PiperProcess:

    name = "Piper em process"
    suporta_sincronia = True

    def __init__(self):

        if not os.path.exists(PIPER_MODEL):
            raise RuntimeError(f"modelo não encontrado: {PIPER_MODEL}")

        from piper import PiperVoice  # Let ImportError propagate to the caller

        self._voice = PiperVoice.load(PIPER_MODEL)

        self.rate = int(self._voice.config.sample_rate)
        self._bytes_por_second = self.rate * 2  # S16_LE, mono

        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._enviadas = 0
        self._concluidas = 0
        self.tocando_ate = 0.0
        self._last_progress = time.time()

        # Volume event schedule: (playback time, level 0..1).
        self._schedule = []
        self._cond = threading.Condition()
        threading.Thread(target=self._emit_levels,daemon=True).start()

        # Smoke test (and model warm-up): if the library version is
        # incompatible, fail HERE so Jarvis can use the
        # Piper executable instead of remaining silent.
        for _ in self._synthesize("Ready."):
            pass

        self.aplay = _open_aplay(self.rate)

        self._thread = threading.Thread(
            target=self._work,
            daemon=True
        )
        self._thread.start()

    def _synthesize(self,text):

        voice = self._voice

        if hasattr(voice,"synthesize_stream_raw"):

            try:
                yield from voice.synthesize_stream_raw(
                    text,
                    sentence_silence=0.2
                )
            except TypeError:
                yield from voice.synthesize_stream_raw(text)

        else:

            for pedaco in voice.synthesize(text):
                yield pedaco.audio_int16_bytes

    def _play(self,data,on_start=None):

        # The audio timeline and scheduled callbacks (card and level) must be set
        # BEFORE writing: write() blocks when aplay’s pipe is full and,
        # for a long phrase, returns only near the END of the chunk. Scheduling after
        # it would delay the card (and orb level) by several seconds.
        with self._lock:

            start_sound = max(time.time(),self.tocando_ate)

            self.tocando_ate = (
                start_sound + len(data) / self._bytes_por_second
            )

            self._last_progress = time.time()

        self._schedule_levels(data,start_sound)

        if on_start is not None:
            self._schedule_action(on_start,start_sound + DELAY_VISUAL)

        for tentativa in range(2):

            try:

                self.aplay.stdin.write(data)
                self.aplay.stdin.flush()

                break

            except (BrokenPipeError,OSError,ValueError):

                if tentativa == 1:
                    raise

                _close(self.aplay)
                self.aplay = _open_aplay(self.rate)

        with self._lock:
            self._last_progress = time.time()

    def _schedule_action(self,action,instant):
        """Dispara `acao` no instant em que o som correspondente toca."""

        wait = max(0.0,instant - time.time())

        temporizador = threading.Timer(wait,_execute_action,args=(action,))
        temporizador.daemon = True
        temporizador.start()

    def _schedule_levels(self,data,start_sound):
        """
        Calcula o volume (RMS) de cada janela de 40 ms do áudio e agenda
        um event "level" para o instant em que ela TOCA, assim o orbe
        bate junto com a voz. Só trabalha se houver interface conectada.
        """

        if not bus.has_clientes():
            return

        amostras = array.array("h")
        amostras.frombytes(data[: len(data) // 2 * 2])

        tamanho = max(1,int(self.rate * WINDOW_LEVEL))

        items = []

        for indice in range(0,len(amostras),tamanho):

            block = amostras[indice:indice + tamanho]

            if not block:
                break

            rms = math.sqrt(sum(x * x for x in block) / len(block))

            instant = (
                start_sound
                + (indice / self.rate)
                + DELAY_VISUAL
            )

            items.append((instant,min(1.0,rms / LEVEL_REFERENCE)))

        # At the end of the chunk, volume returns to zero.
        items.append((
            start_sound + len(data) / self._bytes_por_second + DELAY_VISUAL,
            0.0
        ))

        with self._cond:

            for item in items:
                heapq.heappush(self._schedule,item)

            self._cond.notify()

    def _emit_levels(self):

        while True:

            with self._cond:

                while not self._schedule:
                    self._cond.wait()

                instant,value = self._schedule[0]
                wait = instant - time.time()

                if wait > 0:
                    # can wake sooner if an earlier event arrives
                    self._cond.wait(timeout=wait)
                    continue

                heapq.heappop(self._schedule)

            emit("level",value=round(value,3))

    def _work(self):

        while True:

            item = self._queue.get()

            if item is None:
                return

            text,on_start = item
            pending = on_start

            try:

                for data in self._synthesize(text):
                    # the callback is scheduled inside _play, right at the start
                    action,pending = pending,None
                    self._play(data,action)

            except Exception as e:

                print(f"[!] Erro ao falar: {e}")

            finally:

                # Nothing played; keep the block’s card.
                if pending is not None:
                    _execute_action(pending)

                with self._lock:
                    self._concluidas += 1
                    self._last_progress = time.time()

    def speak(self,text,on_start=None):

        text = clear_text_for_voice(text)

        if not text:

            if on_start is not None:
                _execute_action(on_start)

            return

        with self._lock:
            self._enviadas += 1
            self._last_progress = time.time()

        self._queue.put((text,on_start))

    def wait_end(self,margin=0.2):
        """
        Espera TODAS as frases enviadas serem sintetizadas e o som
        acabar de tocar. A contagem de frases é exata: um intervalo
        enquanto a voz gera a próxima frase não engana mais.
        """

        while True:

            now = time.time()

            with self._lock:
                pending = self._enviadas - self._concluidas
                end = self.tocando_ate
                parado_ha = now - self._last_progress

            if pending <= 0 and now >= end + MARGEM_ALSA:
                break

            if pending > 0 and parado_ha > WITHOUT_PROGRESS_MAX:
                print("[!] A voz parou de responder; seguindo em frente.")
                break

            if not self._thread.is_alive():
                break

            time.sleep(0.03)

        time.sleep(margin)

    def stop(self):

        self._queue.put(None)
        _close(self.aplay)


class PiperCLI:
    """
    Piper -> Python -> aplay (executável `piper` por pipe). O Python
    fica no meio só pra medir o áudio (bytes / taxa por segundo).
    """

    name = "Piper executável (reserva)"
    suporta_sincronia = False

    # No new Piper audio for this long means it has finished.
    IDLE_END = 1.5

    # How long to wait for the first audio from a Piper phrase.
    WAIT_MAX_FIRST_AUDIO = 15.0

    def __init__(self):

        self.proc = None
        self.aplay = None
        self.rate = _model_rate()
        self._bytes_por_second = self.rate * 2
        self._lock = threading.Lock()
        self.tocando_ate = 0.0
        self.last_audio = 0.0
        self.last_request = 0.0
        self.start()

    def _alive(self):

        return bool(
            self.proc and self.proc.poll() is None
            and self.aplay and self.aplay.poll() is None
        )

    def _finish_processs(self):

        _close(self.proc)
        _close(self.aplay)

        self.proc = None
        self.aplay = None

    def start(self):

        if self._alive():
            return

        if not os.path.exists(PIPER_EXEC):
            print(f"[!] Piper não encontrado: {PIPER_EXEC}")
            return

        if not os.path.exists(PIPER_MODEL):
            print(f"[!] Modelo Piper não encontrado: {PIPER_MODEL}")
            return

        self._finish_processs()

        try:

            self.aplay = _open_aplay(self.rate)

            self.proc = subprocess.Popen(
                [PIPER_EXEC,"--model",PIPER_MODEL,"--output_raw"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE
            )

        except Exception as e:

            print(f"[!] Erro ao iniciar Piper: {e}")
            self._finish_processs()
            return

        threading.Thread(
            target=self._forward,
            args=(self.proc,self.aplay),
            daemon=True
        ).start()

    def _forward(self,piper,aplay):

        try:

            while True:

                data = piper.stdout.read1(8192)

                if not data:
                    break

                now = time.time()

                aplay.stdin.write(data)
                aplay.stdin.flush()

                with self._lock:

                    self.tocando_ate = (
                        max(now,self.tocando_ate)
                        + len(data) / self._bytes_por_second
                    )

                    self.last_audio = time.time()

        except Exception:
            pass

    def speak(self,text,on_start=None):

        # There is no way to measure when each text starts, so trigger now. The caller
        # must speak one block at a time (supports_sync=False).
        if on_start is not None:
            _execute_action(on_start)

        text = clear_text_for_voice(text)

        if not text:
            return

        if not self._alive():
            self.start()

        if not self.proc:
            return

        try:

            with self._lock:
                self.last_request = time.time()

            self.proc.stdin.write((text + "\n").encode("utf-8"))
            self.proc.stdin.flush()

        except Exception as e:
            print(f"[!] Erro ao falar: {e}")
            self._finish_processs()

    def wait_end(self,margin=0.2):

        while True:

            now = time.time()

            with self._lock:
                end = self.tocando_ate
                last = self.last_audio
                request = self.last_request

            if not self._alive():
                break

            if request > last:

                if now - request > self.WAIT_MAX_FIRST_AUDIO:
                    break

            elif (
                now - last >= self.IDLE_END
                and now >= end + MARGEM_ALSA
            ):
                break

            time.sleep(0.03)

        time.sleep(margin)

    def stop(self):

        self._finish_processs()


def PersistentPiper():
    """Escolhe o melhor jeito de falar disponível."""

    try:

        voice = PiperProcess()

        print(f"✓ Voz: {voice.name} ({voice.rate} Hz)")

        return voice

    except Exception as e:

        print(
            f"[ℹ] Piper em process indisponível ({e}); "
            f"usando o executável do Piper."
        )

        voice = PiperCLI()

        print(f"✓ Voz: {voice.name} ({voice.rate} Hz)")

        return voice