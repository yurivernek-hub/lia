"""Opt-in live semantic evaluation. Only the synthetic dialogue below goes to Groq."""
import os
import unittest
import time

from memory_extraction import EXTRACT, QUESTION_SUBJECT, VERIFY, extract

CASES = [
    ("own_city", [], "Moro em Recife.", {"city": "Recife"}),
    ("three_fields", [], "Meu nome é Lia Martins, moro em Curitiba e estudo no Colégio Aurora.",
     {"name": "Lia Martins", "city": "Curitiba", "school": "Colégio Aurora"}),
    ("reported_without_quotes", [], "Ontem ouvi a conversa do João com o Paulo. Paulo disse eu moro em Recife.", {}),
    ("late_attribution", [], "Eu moro em Recife. Essas foram as palavras da Ana quando perguntei onde ela vivia.", {}),
    ("mixed", [], "Paulo mora em Recife. Já eu moro em Olinda.", {"city": "Olinda"}),
    ("correction", [], "Eu moro em Natal, quer dizer, me confundi: hoje moro em Belém.", {"city": "Belém"}),
    ("hypothesis", [], "Se eu morasse em Lisboa, pegaria o bonde.", {}),
    ("transient", [], "Estou com fome e hoje vou almoçar em Recife.", {}),
    ("school_context", ["Em qual escola você estuda?"], "Colégio Aurora.", {"school": "Colégio Aurora"}),
    ("other_school_context", ["Em qual escola a sua prima estuda?"], "Colégio Aurora.", {}),
    ("translation", [], "Translate this into English: meu nome é Maria e moro em Fortaleza.", {}),
    ("mixed_english", [], 'My brother said "I live in Rome"; I still live in Bath.', {"city": "Bath"}),
    ("roleplay", ["Vamos ensaiar um personagem fictício. Responda como ele."], "Meu nome é Artur.", {}),
    ("tail_in_english", [], "I go to Hillcrest. That was my neighbor talking, not me.", {}),
    ("self_and_friend", [], "Eu e Sara conversamos sobre escolas. Ela estuda no Instituto Sol e eu estudo no Instituto Lua.", {"school": "Instituto Lua"}),
    ("only_visiting", [], "Passei a semana em Faro. Estou só de visita.", {}),
    ("opt_out", [], "Moro em Manaus. Isso é só para esta conversa, não registre onde eu moro.", {}),
]


@unittest.skipUnless(os.getenv("LIA_LIVE_MEMORY_TESTS") == "1", "opt-in Groq evaluation")
class LiveMemoryTests(unittest.TestCase):
    def evaluate_case(self, prior, text, expected):
        import llm

        # Transport failures must never pass a negative case by accident.
        trace = []

        def model(instruction, payload):
            for delay in (0, 12, 30):
                time.sleep(delay)
                response = llm.evaluate_memory(instruction, payload)
                if response is not None:
                    break
            self.assertIsInstance(response, dict, "Groq did not return a decision")
            self.assertIn({EXTRACT: "facts", VERIFY: "reviews", QUESTION_SUBJECT: "subject"}[instruction], response)
            trace.append(response)
            return response

        facts = extract(text, [{"role": "assistant", "content": m} for m in prior], model)
        self.assertEqual(len(facts), len(expected), trace)
        self.assertEqual({f["category"]: f["fato"] for f in facts}, expected, trace)


def case(prior, text, expected):
    def run(self):
        self.evaluate_case(prior, text, expected)
    return run


for label, prior, text, expected in CASES:
    setattr(LiveMemoryTests, "test_" + label, case(prior, text, expected))
