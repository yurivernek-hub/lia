import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import personal_memory as memory
import memory_extraction as memory_extract


class PersonalMemoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.file = Path(folder.name) / "memories.json"
        private = patch.object(memory, "MEMORY_FILE", self.file)
        private.start()
        self.addCleanup(private.stop)
        self.model = Mock()
        self.chat = memory.MemoryConversation(self.model)

    def decisions(self, facts, rejected=()):
        def model(instruction, payload):
            if instruction == memory_extract.QUESTION_SUBJECT:
                return {"subject":"user", "field":facts[0]["field"]}
            message = json.loads(payload)
            if instruction == memory_extract.EXTRACT:
                return {"act":"disclosure", "facts": facts}
            return {"frame":"real", "reviews": [
                dict(index=i, owner="other" if i in rejected else "user", use="assertion",
                     current=True, durable=True, permitted=True)
                for i in range(len(message["hypotheses"]))
            ]}
        self.model.side_effect = model

    def candidate(self, fact, category="preference", topics=None):
        self.decisions([dict(field=category, value=fact, evidence=fact, topics=topics or [])])
        return self.chat.observe(fact)

    def test_entire_message_and_prior_reach_both_passes(self):
        history = [{"role": "assistant", "content": "Who said this?"}]
        text = "I live in Porto. That is what a friend said."
        self.decisions([dict(field="city", value="Porto", evidence="I live in Porto")], rejected=[0])
        self.assertIsNone(self.chat.observe(text, history))
        self.assertFalse(self.file.exists())
        for call in self.model.call_args_list:
            payload = json.loads(call.args[1])
            self.assertEqual(payload["current_user_message"], text)
            self.assertEqual(payload["prior_conversation"], history)

    def test_long_mixed_turn_saves_only_reviewed_owner(self):
        text = "Yesterday they were talking. " * 180 + "She lives in Porto. I live in Bath."
        self.decisions([dict(field="city", value=city, evidence=city) for city in ("Porto", "Bath")],
                       rejected=[0])
        self.chat.observe(text)
        self.assertEqual(json.loads(self.file.read_text())["person"]["city"]["value"], "Bath")
        self.assertNotIn("Porto", self.file.read_text())
        self.assertEqual(json.loads(self.model.call_args_list[0].args[1])["current_user_message"], text)

    def test_mixed_frame_requires_independent_evidence(self):
        self.model.side_effect = [
            {"act":"mixed", "facts":[dict(field="city", value="Porto", evidence="I live in Porto")]},
            {"frame":"mixed", "excluded_spans":["I live in Porto"],
             "reviews":[dict(index=0, owner="user", use="assertion", current=True, durable=True, permitted=True)]}
        ]
        self.assertIsNone(self.chat.observe("I live in Porto. Those were her words."))
        self.assertFalse(self.file.exists())

    def test_elliptical_answer_must_match_owner_of_prior_question(self):
        self.model.side_effect = [
            {"act":"answer", "facts":[dict(field="school", value="North School", evidence="North School")]},
            {"frame":"real", "reviews":[dict(index=0, owner="user", use="assertion", current=True, durable=True, permitted=True)]},
            {"subject":"other", "field":"school"}
        ]
        prior = [{"role":"assistant", "content":"Where does your cousin study?"}]
        self.assertIsNone(self.chat.observe("North School.", prior))
        self.assertEqual(json.loads(self.model.call_args_list[-1].args[1]), prior)
        self.assertFalse(self.file.exists())

    def test_skip_even_if_extractor_lists_entities(self):
        self.model.return_value = {"act":"other", "facts":[dict(field="city", value="Porto", evidence="Porto")]}
        self.assertIsNone(self.chat.observe("Translate: I live in Porto."))
        self.assertFalse(self.file.exists())

    def test_legacy_personal_note_is_not_promoted_to_profile(self):
        self.file.write_text(json.dumps([dict(id=9, category="personal", source="confirmed",
                                              fato="My friend lives in Rome", data="2025-01-01", topics=[])]))
        self.chat.command("remember that I play piano")
        data = json.loads(self.file.read_text())
        self.assertIsNone(data["person"]["city"])
        self.assertEqual(data["memories"][0]["id"], 9)
        self.assertEqual(data["memories"][0]["value"], "My friend lives in Rome")

    def test_automatic_and_duplicate(self):
        self.assertIn("Saved", self.candidate("Prefiro respostas curtas.", topics=["respostas"]))
        self.assertIsNone(self.chat.observe("Prefiro respostas curtas."))
        record, = memory.records()
        self.assertEqual(record["source"], "automatic")
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)

    def test_person_schema_and_replacement(self):
        text = "Meu nome é Jo Lima, moro em Belém e estudo no Instituto Azul."
        self.decisions([dict(field=k, value=v, evidence=v) for k, v in
                        [("name", "Jo Lima"), ("city", "Belém"), ("school", "Instituto Azul")]])
        self.assertIn("Saved", self.chat.observe(text))
        original = json.loads(self.file.read_text())
        self.assertEqual(original["version"], 2)
        self.assertEqual(original["memories"], [])
        self.assertEqual(original["person"]["name"]["value"], "Jo Lima")
        self.assertEqual(original["person"]["school"]["value"], "Instituto Azul")
        self.decisions([dict(field="city", value="Porto", evidence="Porto")])
        self.chat.observe("Agora moro em Porto")
        current = json.loads(self.file.read_text())["person"]["city"]
        self.assertEqual(current["value"], "Porto")
        self.assertEqual(current["id"], original["person"]["city"]["id"])
        self.assertEqual(memory.relevant("onde eu moro?"), ["city: Porto"])
        self.assertNotIn("Belém", self.file.read_text())
        self.assertIn("Porto", self.chat.command("liste minhas memórias"))
        self.chat.command(f"delete memory {current['id']}")
        self.assertIsNone(json.loads(self.file.read_text())["person"]["city"])

    def test_typed_edit_keeps_field_and_number_then_forget_by_value(self):
        self.decisions([dict(field="school", value="West High", evidence="West High")])
        self.chat.observe("I study at West High.")
        self.decisions([dict(field="school", value="East High", evidence="East High")])
        self.assertIn("updated", self.chat.command("correct memory 1 to East High"))
        entry = json.loads(self.file.read_text())["person"]["school"]
        self.assertEqual((entry["id"], entry["value"], entry["source"]), (1, "East High", "corrected"))
        self.assertIn("deleted", self.chat.command("forget that I study at East High"))

    def test_ask_reject_interrupt_or_restart(self):
        self.chat.mode = "ask"
        for answer in ("Não.", "Question about something else"):
            self.candidate("I play cello", "routine")
            self.chat.command(answer)
            self.assertIsNone(self.chat.command("yes"))
        self.candidate("I play cello", "routine")
        self.assertIsNone(memory.MemoryConversation(self.model).command("yes"))
        self.assertFalse(self.file.exists())
        self.assertIn("Saved", self.chat.command("pode salvar"))
        self.assertEqual(memory.records()[0]["source"], "confirmed")

    def test_invalid_output_or_missing_evidence(self):
        for result in (None, {"act":"disclosure", "facts": "bad"}, {"act":"disclosure", "facts": [{"field": "city", "value": "Rome",
                     "evidence": "Rome"}]}, {"act":"disclosure", "facts": [{"field": {}, "value": "here", "evidence": "here"}]}):
            self.model.return_value = result
            self.assertIsNone(self.chat.observe("I live here"))
        self.assertFalse(self.file.exists())

    def test_veto_or_invalid_review_fails_closed(self):
        candidate = {"field": "city", "value": "Recife", "evidence": "Recife"}
        valid = dict(index=0, owner="user", use="assertion", current=True, durable=True, permitted=True)
        for changes in ({"owner": "other"}, {"use": "translation"}, {"durable": False},
                        {"permitted": False}, {"current": False}, {"current": "true"}, {"index": 4}):
            self.model.side_effect = [{"act":"disclosure", "facts":[candidate]}, {"frame":"real", "reviews":[dict(valid, **changes)]}]
            self.assertIsNone(self.chat.observe("Recife"))
        self.assertFalse(self.file.exists())

    def test_conflict_or_capacity_does_not_partially_write(self):
        self.decisions([dict(field="city", value=v, evidence=v) for v in ("Porto", "Recife")])
        self.assertIsNone(self.chat.observe("Porto ou Recife"))
        self.chat.limit = 1
        self.decisions([dict(field=k, value=v, evidence=v) for k,v in [("name","Ana"),("city","Porto")]])
        self.assertIn("full", self.chat.observe("Ana, Porto"))
        self.assertFalse(self.file.exists())

    def test_credentials(self):
        self.assertIsNone(self.chat.observe("My password is 123"))
        self.model.assert_not_called()

    def test_manual_and_ask_modes(self):
        manual = memory.MemoryConversation(self.model, "manual")
        self.assertIsNone(manual.observe("I prefer brief answers"))
        self.model.assert_not_called()
        self.assertIn("Saved", manual.command("Remember that I play piano"))
        self.chat.mode = "ask"
        self.assertIn("Should I remember", self.candidate("I prefer brief answers", "preference"))
        self.assertEqual(len(memory.records()), 1)

    def test_topic_retrieval(self):
        self.candidate("Sou vegetariana", topics=["recipes", "receitas", "dinner"])
        self.chat.command("save it")
        self.chat.command("Remember that I play violin")
        self.assertEqual(memory.relevant("Me sugira receitas"), ["Sou vegetariana"])
        self.assertEqual(memory.relevant("What about the weather?"), [])
        self.assertEqual(memory.relevant("Help tune my violin"), ["I play violin"])

    def test_edit_delete_and_legacy_ids(self):
        self.file.write_text(json.dumps([{"data": "2020", "fato": "I play cello"}]))
        self.chat.command("guarde que eu estudo japonês")
        self.assertIn("updated", self.chat.command("Corrija a memória 2 para eu estudo francês"))
        self.assertTrue(self.chat.changed_existing)
        self.assertEqual(memory.relevant("japonês"), [])
        self.assertIn("deleted", self.chat.command("delete memory one"))
        self.assertEqual([(i["id"], i["fato"]) for i in memory.records()], [(2, "eu estudo francês")])
        self.assertIn("deleted", self.chat.command("Esqueça que eu estudo francês"))
        self.assertEqual(memory.records(), [])

    def test_ambiguous_capacity_and_corruption(self):
        self.chat.limit = 2
        for fact in ("I play cello", "I play violin", "I play piano"):
            self.chat.command("Remember that " + fact)
        self.assertIn("number", self.chat.command("forget I play"))
        self.assertEqual(len(memory.records()), 2)
        self.file.write_text("{broken")
        self.assertIn("couldn't", self.chat.command("remember that I speak French"))
        self.assertEqual(self.file.read_text(), "{broken")

    def test_concurrent_saves(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: memory.MemoryConversation(self.model).command(
                f"Remember that sample {i}"), range(16)))
        self.assertEqual(len({item["id"] for item in memory.records()}), 16)


if __name__ == "__main__":
    unittest.main()
