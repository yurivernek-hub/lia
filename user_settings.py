"""Private settings and personal memory for this installation."""
import json
import os
import tempfile
from pathlib import Path

from memory_extraction import FIELDS

PROJECT = Path(__file__).resolve().parent
PRIVATE_DIR = Path.home() / ".config" / "jarvisb"
SETTINGS_FILE = PRIVATE_DIR / "settings.json"
MEMORY_FILE = PRIVATE_DIR / "memories.json"
PROFILE_FILE = PRIVATE_DIR / "profile.json"


def write_private_json(path, data, *, create_only=False):
    """Publish complete owner-only files, including on concurrent first launch."""
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            if create_only:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            else:
                os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def initialize_settings():
    PRIVATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    defaults = json.loads((PROJECT / "settings.example.json").read_text(encoding="utf-8"))
    if not SETTINGS_FILE.exists():
        write_private_json(SETTINGS_FILE, defaults, create_only=True)

    # Older releases stored personal data alongside the application. Prefer
    # the files used by the last release, and leave the originals intact.
    for target, prefix, empty in (
        (MEMORY_FILE, "memoria", []),
        (PROFILE_FILE, "perfil", {}),
    ):
        if target.exists():
            continue
        data = empty
        for suffix in ("jarvisb", "jarvis"):
            legacy = PROJECT / f"{prefix}_{suffix}.json"
            if legacy.is_file():
                data = json.loads(legacy.read_text(encoding="utf-8"))
                if not isinstance(data, type(empty)):
                    raise ValueError(f"Unexpected JSON format in {legacy}")
                break
        if target == MEMORY_FILE and not data:
            data = {"version": 2, "person": dict.fromkeys(sorted(FIELDS)), "memories": []}
        write_private_json(target, data, create_only=True)

    settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError(f"Expected a JSON object in {SETTINGS_FILE}")
    for key, default in defaults.items():
        value = settings.get(key, default)
        if not isinstance(value, str):
            raise ValueError(f"Expected text for {key} in {SETTINGS_FILE}")
        settings[key] = value.strip() or default
    return settings
