import os
import re
import unicodedata

from config import PHRASES_FAREWELL


def clear_screen():
    os.system("clear" if os.name != "nt" else "cls")


def clear_text_for_voice(text):
    if not text:
        return ""

    text = re.sub(r"```.*?```","",text,flags=re.DOTALL)
    text = re.sub(r"`.*?`","",text)

    text = "".join(
        c
        for c in text
        if not unicodedata.category(c).startswith("So")
        and not unicodedata.category(c).startswith("Sk")
    )

    # Symbols that the English voice should read as words.
    text = re.sub(r"\$\s?(\d+(?:[.,]\d+)*)",r"\1 dollars",text)
    text = text.replace("%"," percent").replace("&"," and ")

    # Preserve apostrophes (don't, it's) and accented characters.
    text = re.sub(
        r"[^\w\s,.\-!?;:'’áàâãéèêíóôõúçÁÀÂÃÉÈÊÍÓÔÕÚÇ]",
        " ",
        text
    )

    return re.sub(r"\s+"," ",text).strip()


def normalize_text(text):
    text = text.lower().strip()
    text = unicodedata.normalize("NFD",text)

    text = "".join(
        c
        for c in text
        if unicodedata.category(c) != "Mn"
    )

    return re.sub(r"\s+"," ",text)


def detect_farewell(text):

    text_normalized = normalize_text(text)
    text_normalized = re.sub(r"[^a-z0-9' ]"," ",text_normalized)
    text_normalized = re.sub(r"\s+"," ",text_normalized).strip()

    words = text_normalized.split()

    if not words or len(words) > 6:
        return False

    return any(
        re.search(rf"\b{re.escape(phrase)}\b",text_normalized)
        for phrase in PHRASES_FAREWELL
    )