"""
Reconhecimento de voz via Whisper hospedado no Groq (API). Usa o
mesmo cliente e a mesma chave do LLM, então não precisa de nada novo
além do pacote groq.
"""

import os

import groq

import llm
from config import GROQ_STT_MODEL,LANGUAGE_STT
from events import emit

PROMPT_DEFAULT = (
    "Natural spoken conversation. "
    "Recognize questions and commands correctly."
)


def transcribe(path_audio,prompt=None):
    """
    Envia o arquivo de áudio ao Whisper do Groq e devolve o texto
    (string vazia se falhar — o erro é impresso e avisado à interface).
    """

    try:

        with open(path_audio,"rb") as f:
            data = f.read()

        parametros = {
            "file": (os.path.basename(path_audio),data),
            "model": GROQ_STT_MODEL,
            "language": LANGUAGE_STT,
            "response_format": "text",
            "temperature": 0.0,
        }

        if prompt:
            parametros["prompt"] = prompt

        response = llm.GROQ_CLIENT.audio.transcriptions.create(
            **parametros
        )

        return str(response).strip()

    except groq.RateLimitError:

        print("\n[!] Limite de uso do Whisper no Groq atingido.")
        emit("state",state="error",message="Groq limit reached.")

    except groq.AuthenticationError as e:

        print(f"\n[!] Chave do Groq recusada na transcrição: {e}")
        emit("state",state="error",message="Invalid Groq key.")

    except Exception as e:

        print(f"\n[!] Erro na transcrição (Groq): {e}")
        emit("state",state="error",message="Transcription failed.")

    return ""