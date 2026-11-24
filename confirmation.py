"""
Confirmação por voz ou botão (a regra de sempre: só comando com sudo).

Fica separada do fluxo de ferramentas porque dois caminhos usam a mesma
pergunta: a tarefa em primeiro plano (fluxo_ferramentas) e a tarefa em
segundo plano, cujo pedido espera na fila de mensagens até o momento
certo (tarefas_bg). Quem chama garante que é a thread principal: só ela
fala e escuta.
"""

import os
import re

from memory import address_suffix
import stt
from microphone import record_command_pyaudio
from utils import normalize_text
from config import SILENCE_END_CONFIRMATION
from events import emit,bus
from task import Confirmer,YES,NOT,WITHOUT_RESPONSE


def _transcribe_confirmation(file):

    text = stt.transcribe(file)

    if os.path.exists(file):
        os.remove(file)

    return text


class VoiceConfirmer(Confirmer):
    """
    Pergunta em voz alta e espera a decisão por VOZ ou pelo BOTÃO da
    interface (mesma lógica de sempre).
    """

    def __init__(self,voice,mic_stream,prefix=""):

        self.voice = voice
        self.mic_stream = mic_stream
        self.prefix = prefix

    def confirm(self,tool,description,args):

        voice = self.voice

        bus.clear_decision()

        emit(
            "confirmation",
            tool=tool,
            description=description,
            args=args
        )

        voice.speak(
            f"{self.prefix}May I {description}{address_suffix()}? "
            "Say yes to confirm."
        )
        voice.wait_end()

        print("\n🎙 Aguardando confirmação...")

        # The decision can come from either the interface button or voice input.
        decision_button = bus.get_decision()
        file_confirmation = None

        if not decision_button:

            file_confirmation = record_command_pyaudio(
                self.mic_stream,
                timeout_start=6.0,
                silence_max=SILENCE_END_CONFIRMATION,
                interromper=bus.has_decision
            )

            decision_button = bus.get_decision()

        if not file_confirmation and not decision_button:
            return WITHOUT_RESPONSE

        if decision_button:

            text_confirmation = (
                "yes" if decision_button == "confirm" else "cancel"
            )

            if file_confirmation and os.path.exists(file_confirmation):
                os.remove(file_confirmation)

        else:

            text_confirmation = _transcribe_confirmation(file_confirmation)

        print(f"\n> (confirmação) {text_confirmation}\n")

        text_normalized = normalize_text(text_confirmation)
        words_ditas = re.findall(r"[a-z']+",text_normalized)

        words_afirmativas = {
            "yes","yeah","yep","yup","sure","confirm","confirmed",
            "proceed","affirmative","correct","okay","ok","approved",
            "go","do"
        }

        words_negativas = {
            "no","not","nope","don't","dont","cancel","stop","never",
            "negative","wait","abort","nevermind"
        }

        # "no, don't do it" contains "do", so any negation takes precedence.
        autorizado = (
            any(p in words_ditas for p in words_afirmativas)
            and not any(p in words_ditas for p in words_negativas)
        )

        return YES if autorizado else NOT