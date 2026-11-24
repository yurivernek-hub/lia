# Jarvis B entry point. Run this file:
#     python jarvisb.py
# The other modules (config, llm, speech, microphone, etc.) are imported
# from here.

import config  # must come first (environment variables and warnings)

import os
import sys
import time
import re
import random
import subprocess
import importlib.util

import numpy as np
import pyaudio

from openwakeword.model import Model

import llm  # loads/prompts for the Groq key when imported
import stt
import microphone
from config import (
    FORMAT,
    CHANNELS,
    RATE,
    CHUNK,
    WAKE_THRESHOLD,
    SILENCE_END_COMMAND,
    GROQ_STT_MODEL,
    GROQ_MODEL,
    RESPONSES_FAREWELL,
    OPEN_INTERFACE_TOGETHER,
    ARGS_INTERFACE,
    FOLDER_PROJECT,
    OPEN_BACKGROUND_TOGETHER,
    BACKGROUND_IMAGE,
)
from utils import clear_screen,detect_farewell
from speech import PersistentPiper
from memory import (
    address_suffix,
    preferred_addresses,
    detect_preference_address,
)
from personal_memory import MemoryConversation
from tools import looks_like_system_request
from tool_flow import process_with_tools
from voice_control import VoiceController
import background_tasks
import collaboration_voice
import reminders
from morning_routine import execute_routine_morning
from events import bus,emit


clear_screen()

print("=" * 60)
print("                    JARVIS B  (Groq API)")
print("=" * 60)

llm.check_groq()

print("\nConfigurando Groq...")

llm.configure_groq()

print("✓ Groq configurado")

personal_memory = MemoryConversation(
    llm.evaluate_memory, config.USER_SETTINGS["memory_mode"], config.MAX_MEMORIES
)

_forms = preferred_addresses()
if _forms:
    print(f"✓ Formas de tratamento: {', '.join(_forms)}")

print("\nCarregando Wake Word...")

oww_model = Model(
    wakeword_models=[
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "wakeword",
            "hey_lia.onnx"
        )
    ],
    inference_framework="onnx"
)

print("✓ Wake Word carregada")

audio = pyaudio.PyAudio()

mic_stream = audio.open(
    format=FORMAT,
    channels=CHANNELS,
    rate=RATE,
    input=True,
    frames_per_buffer=CHUNK
)

print("\nCalibrando microphone...")

microphone.LEVEL_NOISE_ENVIRONMENT = microphone.measure_noise_environment(mic_stream)

print(f"✓ Ruído environment: {microphone.LEVEL_NOISE_ENVIRONMENT:.0f}")

if microphone.LEVEL_NOISE_ENVIRONMENT > 3000:

    print(
        "[!] Ruído environment muito alto pra um lugar silencioso. "
        "Confira a entrada/ganho do microphone no sistema "
        "(pavucontrol ou alsamixer) — provável ganho/boost no "
        "máximo ou dispositivo de entrada errado."
    )

# Single owner of speech: only the main thread speaks; other threads enqueue
# pending messages for delivery at a safe point (voice_control.py).
voice = VoiceController(PersistentPiper())

manager = background_tasks.start_manager(voice)

# Co-work mode: Lia coordinates while OpenCode writes code (collaboration.py).
colab = collaboration_voice.CollaborationBridge(voice)

# Reminders, alarms, and meeting alerts use the same speech queue.
reminders.start(voice.announce,search_schedule=reminders.search_schedule_today)

process_interface = None
process_background = None

if bus.start():

    print(f"✓ Eventos: socket em {bus.path}")

    path_interface = os.path.join(
        FOLDER_PROJECT,
        "interface",
        "jarvis_ui.py"
    )

    # Native background (C++ / layer-shell), if it has been built.
    path_background = os.path.join(
        FOLDER_PROJECT,
        "background",
        "build",
        "jarvisb-background"
    )

    background_ok = False

    if OPEN_BACKGROUND_TOGETHER and os.access(path_background,os.X_OK):

        try:

            arguments_background = [path_background,"--child"]

            if BACKGROUND_IMAGE:
                arguments_background.append(
                    f"--imagem={os.path.expanduser(BACKGROUND_IMAGE)}"
                )

            process_background = subprocess.Popen(
                arguments_background,
                start_new_session=True
            )

            background_ok = True

            print("✓ Fundo (layer-shell) iniciado")

        except Exception as e:
            print(f"[!] Não consegui abrir o background: {e}")

    elif OPEN_BACKGROUND_TOGETHER:

        print(
            "[ℹ] Fundo em C++ ainda não compilado (veja background/CMakeLists.txt); "
            "usando o environment em janela (reserva)."
        )

    if OPEN_INTERFACE_TOGETHER and os.path.exists(path_interface):

        if importlib.util.find_spec("PySide6"):

            try:

                process_interface = subprocess.Popen(
                    [sys.executable,path_interface,"--child"]
                    + (["--environment-externo"] if background_ok else [])
                    + ARGS_INTERFACE,
                    start_new_session=True
                )

                print("✓ Interface lateral iniciada")

            except Exception as e:
                print(f"[!] Não consegui abrir a interface: {e}")

        else:

            print(
                "[!] Interface desativada: instale o PySide6 no mesmo "
                "environment do Jarvis (pip install PySide6)."
            )

print("\n✓ Tudo pronto.")
print(f"✓ Modelo LLM: {GROQ_MODEL} (via API Groq)")
print(f"✓ Modelo STT: {GROQ_STT_MODEL} (via API Groq)")
print("✓ Wake Word: Hey, Lia")

print("\n" + "=" * 60)

def listen_command():
    """
    Escuta o microphone e transcreve. Devolve o texto, ou None se não
    houve speech útil (aí o Jarvis volta a dormir). Texto digitado na
    interface tem prioridade e interrompe a escuta.
    """

    if bus.has_text():
        return bus.get_text()

    print("\n🎙 Ouvindo...")

    emit("state",state="listening")

    start_capture = time.perf_counter()

    file_audio = microphone.record_command_pyaudio(
        mic_stream,
        timeout_start=5.0,
        silence_max=SILENCE_END_COMMAND,
        interromper=bus.has_text
    )

    time_capture = (time.perf_counter() - start_capture) * 1000

    if not file_audio:

        if bus.has_text():
            return bus.get_text()

        print("\n💤 Nenhuma speech detectada.")

        return None

    print(f"\n⏳ Processando [captura: {time_capture:.0f} ms]")

    emit("state",state="transcribing")

    start_stt = time.perf_counter()

    command_text = stt.transcribe(
        file_audio,
        prompt=stt.PROMPT_DEFAULT
    )

    time_stt = (time.perf_counter() - start_stt) * 1000

    if os.path.exists(file_audio):
        os.remove(file_audio)

    print(f"[⚡ STT: {time_stt:.0f} ms]")

    text_verificacao = re.sub(
        r"[^a-zA-ZÀ-ÿ0-9]","",command_text
    )

    if not command_text or len(text_verificacao) <= 1:

        print("\n[!] Não entendi.")

        return None

    return command_text


modo_conversacao = False

try:

    execute_routine_morning(voice)

    while True:

        command_digitado = None

        if not modo_conversacao:

            print("\n🎧 Aguardando 'Hey, Lia'...")

            emit("state",state="sleeping")

            oww_model.reset()

            voice.state = "ocioso"

            wake_word_detected = False
            maior_score = 0.0

            while not wake_word_detected:

                # The user typed in the interface instead of using the wake word.
                if bus.has_text():
                    break

                data = mic_stream.read(
                    CHUNK,
                    exception_on_overflow=False
                )

                # Something is pending (a background task has finished...) and
                # no one is speaking: deliver it. Never interrupt speech.
                voice.sense(data)

                if voice.should_deliver():

                    voice.deliver(
                        mic_stream,
                        oww_model,
                        context="ocioso",
                        state_final="sleeping"
                    )

                    continue

                pcm = np.frombuffer(data,dtype=np.int16)

                oww_model.predict(pcm)

                for mdl in oww_model.prediction_buffer.keys():

                    score = oww_model.prediction_buffer[mdl][-1]

                    if score > maior_score:
                        maior_score = score

                    if score >= WAKE_THRESHOLD:
                        wake_word_detected = True
                        break

            if bus.has_text() and not wake_word_detected:

                command_digitado = bus.get_text()
                modo_conversacao = True

            else:

                print(f"\n✓ Wake word detectada [score: {maior_score:.3f}]")

                emit("state",state="awakened")

                microphone.LEVEL_NOISE_ENVIRONMENT = microphone.measure_noise_environment(
                    mic_stream,
                    seconds=0.3,
                    warmup=0.0
                )

                # Use the configured Piper voice for the wake response.
                # The legacy recording contains the old male Portuguese greeting.
                voice.speak(f"Yes{address_suffix()}?")
                voice.wait_end()

                time.sleep(0.15)

                modo_conversacao = True

        if command_digitado is None:
            command_digitado = bus.get_text() if bus.has_text() else None

        if command_digitado is not None:
            command_text = command_digitado
        else:
            command_text = listen_command()

        if command_text is None:
            personal_memory.pending = None

            # Co-work is open (or opening): Lia does NOT go back to sleep after
            # silence. Deliver queued narration or results
            # and keep listening. Only "that's all"/end closes the session.
            if colab.active or colab.opening:

                voice.state = "conversa"

                voice.deliver(mic_stream,oww_model,context="fim_de_turno")

                modo_conversacao = True

                continue

            modo_conversacao = False
            continue

        print(f"\n> {command_text}\n")

        voice.state = "turno"

        emit("user_speech",text=command_text)

        if detect_farewell(command_text):
            personal_memory.pending = None

            response_farewell = random.choice(RESPONSES_FAREWELL)

            print(f"[👋 Despedida detectada: \"{command_text}\"]")
            print(response_farewell)

            emit("state",state="speaking")
            emit("response_end",text=response_farewell)

            voice.speak(response_farewell)
            voice.wait_end()

            modo_conversacao = False

            print("\n🎧 Voltando a aguardar 'Hey, Lia'...")

            continue

        prior_turns = list(llm.history)
        response = personal_memory.command(command_text, prior_turns)
        if personal_memory.changed_existing:
            # Old assistant replies can repeat a fact after a correction or deletion.
            llm.history.clear()

        # A direct address preference is an explicit request even in manual mode.
        if response is None and personal_memory.mode == "manual" and detect_preference_address(command_text):
            response = personal_memory.explicit_address(command_text, prior_turns)

        analyze_turn = response is None
        if response is None:
            response = colab.handle(command_text)
        if response is None:
            response = manager.respond_about_tasks(command_text)

        executado = response is not None
        if executado:

            print(response)

            start_voice = time.perf_counter()

            emit("state",state="speaking")
            emit("response_end",text=response)

            voice.speak(response)
            voice.wait_end()

            time_voice = (time.perf_counter() - start_voice) * 1000

            print(f"[🔊 Voz: {time_voice:.0f} ms]")

        else:

            if background_tasks.request_second_plan(command_text):

                # "...in the background", "tell me when you're done"
                process_with_tools(
                    command_text,
                    voice,
                    mic_stream,
                    second_plan=True
                )

            elif looks_like_system_request(command_text):

                process_with_tools(command_text,voice,mic_stream)

            else:

                if llm.process_llm_e_speak(command_text,voice) == "ferramentas":

                    # The model wanted to search/use a tool: retry with tools enabled.
                    process_with_tools(command_text,voice,mic_stream)

        # End of turn: Lia has just responded. If a background task
        # finished, announce it now (only if the user is not already
        # speaking), then reopen the microphone.
        voice.state = "conversa"

        voice.deliver(mic_stream,oww_model,context="fim_de_turno")

        if analyze_turn:
            notice = personal_memory.observe(command_text, prior_turns)
            if notice:
                emit("state", state="speaking")
                emit("response_end", text=notice)
                voice.speak(notice)
                voice.wait_end()

        print("\n🎙 Pode continuar falando...")

        modo_conversacao = True

except KeyboardInterrupt:

    print("\n\nEncerrando Jarvis B...")

except Exception as e:

    print(f"\n[!] Erro capturado: {e}")

finally:

    try:
        colab.close_silent()
    except Exception:
        pass

    try:
        manager.finish()
    except Exception:
        pass

    try:
        voice.stop()
    except Exception:
        pass

    try:
        if process_interface:
            process_interface.terminate()
    except Exception:
        pass

    try:
        if process_background:
            process_background.terminate()
    except Exception:
        pass

    try:
        bus.stop()
    except Exception:
        pass

    try:
        mic_stream.stop_stream()
        mic_stream.close()
    except Exception:
        pass

    try:
        audio.terminate()
    except Exception:
        pass

    print("Jarvis B encerrado.")
