import os
import random
import json

from config import (
    USER_SETTINGS,
    PROFILE_FILE,
    PATTERNS_ADDRESS,
)
from personal_memory import records


def load_profile():

    if not os.path.exists(PROFILE_FILE):
        return {}

    try:

        with open(PROFILE_FILE,"r",encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data,dict):
            return {}

        return data

    except Exception:
        return {}


def learned_identity():
    try:
        return {r["category"]: r["fato"] for r in records() if r["category"] in {"name", "form_of_address"}}
    except (ValueError, OSError):
        return {}


def preferred_addresses(learned=None):
    """Spoken preferences take precedence over installation defaults."""
    if learned is None:
        learned = learned_identity()
    if "form_of_address" in learned:
        return [learned["form_of_address"]]
    saved = load_profile().get("forma_tratamento")
    candidates = saved if isinstance(saved, list) else [saved]
    forms = [value.strip() for value in candidates if isinstance(value, str) and value.strip()]
    if forms:
        return forms
    default = USER_SETTINGS["form_of_address"] or learned.get("name") or USER_SETTINGS["name"]
    return [default] if default else []


def address_suffix():
    forms = preferred_addresses()
    return f", {random.choice(forms)}" if forms else ""


def detect_preference_address(text):

    candidatos = []

    for default in PATTERNS_ADDRESS:

        match = default.fullmatch(text.strip().rstrip(".!"))
        if match:
            candidatos.append((match.start(),match.group(1)))

    if not candidatos:
        return None

    candidatos.sort(key=lambda item: item[0])

    return candidatos[-1][1].strip().capitalize()
