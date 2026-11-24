"""
Teste OFFLINE do collaboration.py: sem OpenCode e sem rede.
Reproduz os events REAIS gravados em opencode_events.jsonl contra um
repositório git temporário e confere decisões, pontos de volta e voltar.

    python collaboration_offline.py [opencode_events.jsonl]
"""
import json, os, queue, sys, tempfile, threading, time, subprocess

import collaboration as C

ARQ = sys.argv[1] if len(sys.argv) > 1 else "opencode_events.jsonl"
REAIS = [json.loads(l) for l in open(ARQ)]
ORIGIN = next(
    event["properties"]["info"]["directory"] for event in REAIS
    if event.get("type") == "session.created"
    and event.get("properties", {}).get("info", {}).get("directory")
)


class FakeClient:
    def __init__(self, folder):
        self.folder, self.events, self.enviados, self.abortos = folder, queue.Queue(), [], 0
        self.scenario, self.reproduzindo = None, False

    def sessions(self): return []
    def create_session(self, t=None): return "ses_fake"
    def start_events(self): pass
    def stop_events(self): pass
    def abort(self, s): self.abortos += 1
    def last_response(self, s): return "Tests pass: 2 tests OK."
    def respond_permission(self, *a, **k): pass

    def send(self, session, text, model=None, agent=None):
        self.enviados.append(text)
        if self.reproduzindo or self.scenario is None:
            return
        self.reproduzindo = True
        threading.Thread(target=self._reproduzir, daemon=True).start()

    def _reproduzir(self):
        for ev in REAIS:
            if ev.get("_cenario") != self.scenario:
                continue
            ev = json.loads(json.dumps(ev).replace(ORIGIN, self.folder))
            ev = json.loads(json.dumps(ev).replace(
                ev.get("properties", {}).get("sessionID", "@@"), "ses_fake"))
            p = ev.get("properties", {})
            if ev["type"] == "file.edited":      # materialize the file
                open(p["file"], "w").write("conteudo\n")
            self.events.put(ev)
            time.sleep(0.002)
        self.reproduzindo = False


class FakeServer:
    def __init__(self, c): self.c = c
    def start(self): pass
    def client(self): return self.c
    def stop(self): pass


def llm(system, user, narrator=False):
    if system == C.SYSTEM_NARRATOR: return "Criei os arquivos."
    if system == C.SYSTEM_SUMMARY: return "Done, os testes passaram."
    if system == C.SYSTEM_CONFIRMATION:
        return json.dumps({"response": "sim" if "sim" in user.split("User reply:")[1] else "outro"})
    f = user.split("USER SAID (transcript):")[1].strip()
    if "hello" in f:
        return json.dumps({"acao": "instruir", "confianca": .95, "resumo": "criar hello",
                           "prompt": "Create hello.py and a test, then run it."})
    if "volta" in f and "talvez" not in f:
        return json.dumps({"acao": "voltar", "confianca": .95, "steps": 1})
    if "talvez" in f:
        return json.dumps({"acao": "voltar", "confianca": .6, "steps": 1})
    if "piada" in f:
        return json.dumps({"acao": "conversa", "confianca": .9})
    return json.dumps({"acao": "conversa", "confianca": .1})


def wait(cond, t=5):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.05)
    return False


falhas = []
def check(name, ok):
    print(("  ok   " if ok else "  FALHA ") + name)
    if not ok: falhas.append(name)


folder = tempfile.mkdtemp(prefix="colab_")
subprocess.run(["git", "init", "-q"], cwd=folder)
open(os.path.join(folder, "antigo.txt"), "w").write("ja existia\n")

cli = FakeClient(folder)
falas, questions, concl, feed = [], [], [], []
col = C.Collaboration(
    folder, llm, server=FakeServer(cli),
    ao_narrate=falas.append, ao_ask=questions.append,
    ao_concluir=concl.append, ao_feed=feed.append,
    file_state=os.path.join(folder, "..", "colab_estado_teste.json"),
)
C.INTERVALO_NARRATION = 0
col.start(resume=False)
check("ponto inicial criado", len(col.history) == 1)
check("antigo.txt entrou no ponto inicial", "antigo.txt" in subprocess.run(
    ["git", "ls-files"], cwd=folder, capture_output=True, text=True).stdout)

print("\n1) instrução")
cli.scenario = "1"
r = col.process_speech("cria um hello com teste")
check("interpretou como instruir", r["acao"] == "instruir")
check("mandou o prompt da Lia, não a speech", cli.enviados[0].startswith("Create hello.py"))
check("terminou e avisou", wait(lambda: concl))
check("falou a conclusão", concl and "Done" in concl[0]["speech"])
check("listou os 2 arquivos", concl and len(concl[0]["arquivos"]) == 2)
check("narrou durante o trabalho", len(falas) >= 1)
check("criou ponto de volta", len(col.history) == 2 and col.history[1]["label"] == "criar hello")
check("ocioso no fim", not col.busy)
check("feed teve arquivo e comando", {"arquivo", "command"} <= {i["type"] for i in feed})

print("\n2) conversa não vai pro agente")
n = len(cli.enviados)
r = col.process_speech("hahaha conta uma piada")
check("conversa", r["acao"] == "conversa" and len(cli.enviados) == n)

print("\n3) voltar com certeza")
check("arquivo existe antes", os.path.exists(os.path.join(folder, "hello.py")))
r = col.process_speech("volta isso")
check("voltou", r["acao"] == "voltar" and not os.path.exists(os.path.join(folder, "hello.py")))
check("antigo.txt preservado", os.path.exists(os.path.join(folder, "antigo.txt")))
check("histórico encolheu", len(col.history) == 1)
check("aviso pendente para o OpenCode", col._warning and "reverted" in col._warning)

print("\n4) instruções em fila -> um ponto por instrução")
cli.scenario = "3"
concl.clear()
col.process_speech("cria um hello a")
col.instruct("Also create b.txt with beta.", "criar b.txt")
check("aviso de volta foi junto do prompt", "reverted" in cli.enviados[-2])
check("aviso consumido", col._warning is None)
check("terminou", wait(lambda: concl))
rot = [p["label"] for p in col.history]
print("     pontos:", rot)
check("2 pontos novos (um por instrução)", len(col.history) == 3)

print("\n5) voltar com dúvida pergunta antes")
r = col.process_speech("talvez volta")
check("perguntou", r["acao"] == "perguntar" and questions)
r = col.process_speech("sim")
check("confirmou e voltou", r["acao"] == "voltar" and len(col.history) == 2)

print("\n6) estado")
check("texto de estado", "touched" in col.state_speakable() and "ran" in col.state_speakable())

col.finish()
print("\n" + ("TUDO OK" if not falhas else f"FALHAS: {falhas}"))
sys.exit(1 if falhas else 0)
