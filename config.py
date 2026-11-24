import os

os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import re
import warnings

import pyaudio

from user_settings import MEMORY_FILE, PROFILE_FILE, PRIVATE_DIR, initialize_settings

USER_SETTINGS = initialize_settings()

warnings.filterwarnings("ignore")

PIPER_EXEC = os.path.expanduser("~/.venvs/ovos/bin/piper")
PIPER_MODEL = os.path.expanduser(
    "~/Lia/voices/hfc_female/en_US-hfc_female-medium.onnx"
)

# Whisper is hosted by Groq. "whisper-large-v3-turbo" is faster;
# "whisper-large-v3" provides maximum accuracy.
GROQ_STT_MODEL = "whisper-large-v3-turbo"

# Groq key: the GROQ_API_KEY environment variable or this file
# (created automatically on first launch with permission 600).
# Get a free key at: https://console.groq.com/keys
FILE_KEY_GROQ = str(PRIVATE_DIR / "env")

# GPT-OSS 120B: a reasoning model hosted by Groq. Reasoning tokens
# count toward the output limit, so the limit has extra headroom and
# the reasoning effort is set to "low".
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_MAX_OUTPUT_TOKENS = 1024
GROQ_REASONING_EFFORT = "low"

# Application resources are stored beside jarvisb.py.
FOLDER_PROJECT = os.path.dirname(os.path.abspath(__file__))

MAX_MEMORIES = 40

# Jarvis language (speech, speech recognition, and commands).
LANGUAGE_STT = USER_SETTINGS["speech_language"]

PATTERNS_ADDRESS = [
    re.compile(r"(?:me chame de|pode me chamar de)\s+([A-Za-zÀ-ÿ]+)", re.IGNORECASE),
    re.compile(
        r"\bcall\s+me\s+(?!back|later|tomorrow|please|now|when|if|up)"
        r"([A-Za-zÀ-ÿ]+)",
        re.IGNORECASE
    ),
]

# These apply only when the entire utterance is short (up to 6 words).
PHRASES_FAREWELL = [
    "goodbye","good bye","bye","bye bye","see you","see you later",
    "see you tomorrow","talk to you later","that's all","thats all",
    "that will be all","that is all","good night","go to sleep",
    "shut down","power down","stand down","we're done","we are done",
    "that's it for today"
]

RESPONSES_FAREWELL = [
    "Goodbye.",
    "Until next time.",
    "Very good.",
]

FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
CHUNK = 1024

WAKE_THRESHOLD = 0.04

# Amount of silence (in seconds) that ends speech capture. A larger value means
# you can pause to think without being cut off. It used to be 0.9 to
# save time; the API is fast enough that this is no longer needed.
SILENCE_END_COMMAND = 1.8

# Confirmations ("yes"/"no") are short, so they need less wait time.
SILENCE_END_CONFIRMATION = 1.0

MAX_HISTORY = 20

# System-tool sandbox
FOLDER_WORK = os.path.expanduser("~/jarvisb_workspace")

FOLDERS_ALLOWED = [
    FOLDER_WORK,
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Music"),
    os.path.expanduser("~/Pictures"),
    os.path.expanduser("~/Videos"),
    os.path.expanduser("~/.local/share/Trash"),
]

os.makedirs(FOLDER_WORK,exist_ok=True)

FOLDERS_NAMED = {
    "downloads": os.path.expanduser("~/Downloads"),
    "desktop": os.path.expanduser("~/Desktop"),
    "documents": os.path.expanduser("~/Documents"),
    "documentos": os.path.expanduser("~/Documents"),
    "music": os.path.expanduser("~/Music"),
    "musica": os.path.expanduser("~/Music"),
    "música": os.path.expanduser("~/Music"),
    "pictures": os.path.expanduser("~/Pictures"),
    "imagens": os.path.expanduser("~/Pictures"),
    "fotos": os.path.expanduser("~/Pictures"),
    "videos": os.path.expanduser("~/Videos"),
    "vídeos": os.path.expanduser("~/Videos"),
    "trash": os.path.expanduser("~/.local/share/Trash"),
    "lixeira": os.path.expanduser("~/.local/share/Trash"),
    "workspace": FOLDER_WORK,
    "jarvisb_workspace": FOLDER_WORK,
}

# Floating interface (separate process in interface/jarvis_ui.py).
# When True, jarvisb.py opens the interface too (if PySide6 is
# installed). It runs natively on Wayland; KDE Plasma handles placement and
# "always on top" through a KWin rule created automatically. Only if
# something fails, try ARGS_INTERFACE = ["--xcb"] (XWayland).
OPEN_INTERFACE_TOGETHER = True

# Native background (C++ / layer-shell): if background/build/jarvisb-background
# exists, jarvisb.py launches it too. Otherwise, it uses the windowed
# fallback. BACKGROUND_IMAGE = "" gives a plain dark background (no extra memory);
# or set an image path, e.g. "~/Pictures/jarvis.png".
OPEN_BACKGROUND_TOGETHER = True
BACKGROUND_IMAGE = ""
ARGS_INTERFACE = []

# Web search (the web_search tool) uses Groq’s
# "compound" system, which searches the web autonomously. It uses the same
# GROQ_API_KEY and is free within the account limits. "compound-mini"
# is faster; "groq/compound" runs multiple searches per question.
# Weather and news widgets. CITY_DEFAULT is used when you ask about the
# weather without naming a city. NEWS_REGION can be "pt-BR" or "en-US" (the
# headlines appear in that region’s language; Jarvis summarizes them in English).
CITY_DEFAULT = USER_SETTINGS["weather_city"]
NEWS_REGION = USER_SETTINGS["news_region"]

SEARCH_MODEL = "groq/compound-mini"
# If the account cannot access compound (404), use the
# search built into this model (the same one used for chat).
SEARCH_MODEL_ALTERNATIVO = "openai/gpt-oss-120b"
SEARCH_MAX_RESULTS = 5
# None = no time limit; otherwise, this is a number of seconds.
SEARCH_TIMEOUT = None