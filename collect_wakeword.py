"""Collect local Hey Lia recordings for OpenWakeWord fine-tuning."""

from datetime import datetime
import os
from pathlib import Path
import wave

import numpy as np
import pyaudio


RATE = 16000
CHUNK = 1024
CHANNELS = 1
FORMAT = pyaudio.paInt16
DURACAO = 2.5
AMOSTRAS_TREINO = 30
AMOSTRAS_VALIDACAO = 10
MAX_TENTATIVAS_POR_SAMPLE = 3
FOLDER_DATA = Path.home() / ".cache/jarvisb-wakeword/output/hey_lia"


def record_sample(stream, destination):
    input(
        f"Enter para gravar {destination.name}; diga 'Hey, Lia' uma vez: "
    )

    print("Gravando agora...",flush=True)
    stream.start_stream()

    try:
        blocks = [
            stream.read(CHUNK,exception_on_overflow=False)
            for _ in range(round(RATE * DURACAO / CHUNK))
        ]
    finally:
        stream.stop_stream()

    audio = np.frombuffer(b"".join(blocks),dtype=np.int16)
    audio_int32 = audio.astype(np.int32)
    pico = int(np.max(np.abs(audio_int32)))
    amostras_clipadas = int(np.count_nonzero(
        (audio == np.iinfo(np.int16).max)
        | (audio == np.iinfo(np.int16).min)
    ))

    print(
        f"Nível gravado: pico {pico}/32767; "
        f"amostras saturadas: {amostras_clipadas}",
        flush=True
    )

    if pico < 500:
        print("Áudio muito baixo; confira se o microfone correto está ativo.")
        return False

    if amostras_clipadas >= 8:
        print(
            "Áudio clipado. Reduza o ganho de entrada do microfone "
            "no pavucontrol/alsamixer e tente novamente."
        )
        return False

    with wave.open(str(destination),"wb") as file:
        file.setnchannels(CHANNELS)
        file.setsampwidth(2)
        file.setframerate(RATE)
        file.writeframes(audio.tobytes())

    print(f"Salvo: {destination.name} (pico {pico}/32767)")
    return True


def record_group(audio_stream,name_folder,count,prefix):
    destination = FOLDER_DATA / name_folder
    destination.mkdir(parents=True,exist_ok=True)

    for indice in range(1,count + 1):
        name = f"user_{prefix}_{indice:03d}.wav"
        path = destination / name

        for _ in range(MAX_TENTATIVAS_POR_SAMPLE):
            if record_sample(audio_stream,path):
                break
        else:
            raise RuntimeError(
                f"Não consegui gravar {name} após "
                f"{MAX_TENTATIVAS_POR_SAMPLE} tentativas; "
                "a coleta foi encerrada."
            )


def main():
    identificador = datetime.now().strftime("%Y%m%d_%H%M%S")
    audio = pyaudio.PyAudio()
    stream = None

    try:
        stream = audio.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK,
            start=False
        )

        print(
            f"As gravações ficam somente em {FOLDER_DATA}. "
            "Fale naturalmente, variando um pouco distância e volume. "
            "Cada gravação deve conter apenas 'Hey, Lia'."
        )

        print(f"\nTreino: {AMOSTRAS_TREINO} gravações")
        record_group(
            stream,
            "positive_train",
            AMOSTRAS_TREINO,
            f"{identificador}_train"
        )

        print(f"\nValidação: {AMOSTRAS_VALIDACAO} gravações diferentes")
        record_group(
            stream,
            "positive_test",
            AMOSTRAS_VALIDACAO,
            f"{identificador}_test"
        )

    except KeyboardInterrupt:
        print("\nColeta interrompida; as amostras já salvas foram mantidas.")

    except RuntimeError as error:
        print(f"\n[!] {error}")

    finally:
        if stream is not None:
            stream.close()
        audio.terminate()


if __name__ == "__main__":
    main()