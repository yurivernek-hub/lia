import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPT_FILES = (
    "llm.py", "task.py", "personality.py", "morning_routine.py",
    "web_search.py", "stt.py", "reminders.py",
)


class PromptPrivacyTests(unittest.TestCase):
    def test_only_creator_identity_is_hard_coded(self):
        source = {name: (ROOT / name).read_text(encoding="utf-8") for name in PROMPT_FILES}
        all_prompt_source = "\n".join(source.values())
        self.assertIn("Yuri Vernek created Lia", source["llm.py"])
        for filename, text in source.items():
            if filename == "llm.py":
                text = text.replace("Yuri Vernek created Lia", "")
                text = text.replace("yurivernek-hub/LIA", "")
            self.assertNotRegex(text, r"(?i)\bYuri\b|\bVernek\b|Uberl.ndia|/home/[A-Za-z0-9_.-]+")
        self.assertIn('"name": learned.get("name", USER_SETTINGS["name"])', source["llm.py"])
        self.assertIn('"preferred_weather_city": USER_SETTINGS["weather_city"]', source["llm.py"])
        self.assertIn('"forms_of_address": preferred_addresses(learned)', source["llm.py"])
        self.assertNotIn("Uberl" + "ândia", all_prompt_source)

    def test_private_data_files_are_ignored(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for path in ("/settings.json", "/profile.json", "/memories.json"):
            self.assertIn(path, ignored)


if __name__ == "__main__":
    unittest.main()
