import time
import wave
from collections import deque

import numpy as np
import pyaudio

from config import FORMAT,CHANNELS,RATE,CHUNK
from events import emit

# End of speech: silence is measured relative to the volume of
# YOUR voice (not just ambient noise), and a single click does not
# delay the end; sustained speech is required to reset the silence counter.
FRACAO_SPEECH_SILENCE = 0.22
BLOCKS_VOICE_FOR_RESETAR = 1.2

# Calibrated at runtime (jarvisb.py assigns this attribute).
LEVEL_NOISE_ENVIRONMENT = 0.0


def energy_block(pcm):
    """
    Energia média do bloco SEM o offset DC (nível constante
    deslocado do zero, comum em alguns microfones).
    """

    return float(np.abs(pcm - pcm.mean()).mean())


def measure_noise_environment(stream,seconds=1.0,warmup=0.5):

    # Discard the first few warm-up seconds: right after opening the
    # stream, clicks/noise can inflate the calibration reading.
    for _ in range(int((RATE / CHUNK) * warmup)):
        stream.read(CHUNK,exception_on_overflow=False)

    energias = []
    picos = []
    dcs = []
    count = int((RATE / CHUNK) * seconds)

    for _ in range(count):

        data = stream.read(
            CHUNK,
            exception_on_overflow=False
        )

        pcm = np.frombuffer(
            data,
            dtype=np.int16
        ).astype(np.float32)

        energias.append(energy_block(pcm))
        picos.append(float(np.abs(pcm).max()))
        dcs.append(float(pcm.mean()))

    if warmup > 0:

        print(
            f"[🎙 Diagnóstico mic: pico {max(picos):.0f}/32767 | "
            f"offset DC médio {np.mean(dcs):.0f}]"
        )

    return float(np.median(energias))


def discard_buffer(stream,extra=0.2):
    """
    Joga fora o áudio acumulado no buffer do microfone enquanto o
    Jarvis falava (a própria voz dele), mais "extra" segundos de
    reverberação, antes de começar a ouvir de verdade.
    """

    try:

        for _ in range(400):

            if stream.get_read_available() < CHUNK:
                break

            stream.read(CHUNK,exception_on_overflow=False)

        for _ in range(int((RATE / CHUNK) * extra)):
            stream.read(CHUNK,exception_on_overflow=False)

    except Exception:
        pass


def level_normalized(energy):
    """Energia do bloco acima do ruído ambiente, de 0 a 1."""

    return max(0.0,min(1.0,(energy - LEVEL_NOISE_ENVIRONMENT) / 3000.0))


def record_command_pyaudio(
    stream,
    timeout_start=5.0,
    silence_max=1.8,
    interromper=None,
    discard_start=0.2
):

    discard_buffer(stream,discard_start)

    threshold_start = max(
        LEVEL_NOISE_ENVIRONMENT * 1.65,
        LEVEL_NOISE_ENVIRONMENT + 250.0
    )

    threshold_silence = max(
        LEVEL_NOISE_ENVIRONMENT * 1.12,
        LEVEL_NOISE_ENVIRONMENT + 80.0
    )

    # Never let the thresholds exceed what the microphone can
    # capture (int16 tops out at 32767), or speech could never exceed them.
    threshold_start = min(threshold_start,12000.0)
    threshold_silence = min(threshold_silence,threshold_start * 0.7)

    print(
        f"\n[🎙 Ruído: {LEVEL_NOISE_ENVIRONMENT:.0f} | "
        f"Início: {threshold_start:.0f} | "
        f"Silêncio: {threshold_silence:.0f}]"
    )

    frames = []
    pre_buffer = deque(maxlen=4)

    # Energy from real speech blocks (above the start threshold):
    # this capture’s reference level for your voice.
    energias_speech = []
    blocks_voice = 0

    start = time.perf_counter()
    start_silence = None
    speaking = False
    blocks_speech = 0

    while True:

        data = stream.read(
            CHUNK,
            exception_on_overflow=False
        )

        pcm = np.frombuffer(
            data,
            dtype=np.int16
        ).astype(np.float32)

        # Allow capture to be cancelled externally (for example, by the interface button).
        if interromper and interromper():
            return None

        energy = energy_block(pcm)
        now = time.perf_counter()

        emit("level",value=round(level_normalized(energy),3))

        if not speaking:

            pre_buffer.append(data)

            if energy >= threshold_start:
                blocks_speech += 1
            else:
                blocks_speech = 0

            # Three consecutive blocks (about 90 ms each): a single noise spike
            # is not enough to trigger capture.
            if blocks_speech >= 3:

                speaking = True
                start_silence = None
                energias_speech.append(energy)
                blocks_voice = BLOCKS_VOICE_FOR_RESETAR

                frames.extend(pre_buffer)
                pre_buffer.clear()

                print("\n🗣 [Capturando...]")

            if now - start >= timeout_start:
                return None

        else:

            frames.append(data)

            if energy >= threshold_start:
                energias_speech.append(energy)

            # Reference = typical speech volume (80th percentile); below
            # a fraction of that level counts as silence, even in a noisy environment.
            ordenadas = sorted(energias_speech)
            reference = ordenadas[int(len(ordenadas) * 0.8)]

            threshold_end = max(
                threshold_silence,
                reference * FRACAO_SPEECH_SILENCE
            )

            if energy >= threshold_end:
                blocks_voice += 1
            else:
                blocks_voice = 0

            if blocks_voice >= BLOCKS_VOICE_FOR_RESETAR:

                # Sustained voice: still speaking.
                start_silence = None

            else:

                if start_silence is None:
                    start_silence = now

                elif now - start_silence >= silence_max:
                    break

    if not frames:
        return None

    file_wav = "/tmp/jarvisb_comando.wav"

    with wave.open(file_wav,"wb") as wf:

        wf.setnchannels(CHANNELS)
        wf.setsampwidth(
            pyaudio.get_sample_size(FORMAT)
        )
        wf.setframerate(RATE)
        wf.writeframes(
            b"".join(frames)
        )

    return file_wav