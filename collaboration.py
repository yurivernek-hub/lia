"""
collaboration.py - modo "co-work" da Lia com o OpenCode.

A Lia continua sendo a voz e a coordenadora. O OpenCode programa. Este módulo
é a bridge:

  speech do usuário --(Lia interpreta)--> instruir | parar | voltar | estado | conversa | encerrar
  instruir --(Lia ESCREVE o próprio prompt, nunca repassa a speech)--> OpenCode
  OpenCode --(events ao vivo)--> narração curta + feed da "ilha" + pontos de volta

Decisões (combinadas com o usuário):
  - o servidor do OpenCode só sobe quando a colaboração é aberta;
  - cada instrução vira um PONTO (commit git) num ramo próprio "lia/<sessão>",
    então "voltar" é sempre possível e não mexe no ramo original;
  - "voltar": certeza (>= 0.85) volta; dúvida (0.5-0.85) pergunta; senão é conversa;
  - speech durante o trabalho entra na fila (o OpenCode já enfileira sozinho);
    só "parar" e "voltar" interrompem;
  - a Lia narra por passo (arquivo escrito, comando rodado), com um narrador
    que pode usar outra chave do Groq (GROQ_API_KEY_NARRADOR);
  - a sessão fica salva e pode ser retomada.

Só biblioteca padrão (usa groq/llm da Lia apenas em `llm_padrao`).
O LLM é injetado: `llm(sistema, usuario, narrador=False) -> str`, o que deixa
o módulo testável sem rede.
"""

import json
import os
import re
import subprocess
import threading
import time
import queue
from collections import deque

from opencode_client import (
    OpenCodeServer,
    OpenCodeError,
    event_type,
    session_of_event,
    event_idle,
    event_permission,
)

FILE_STATE = os.path.expanduser("~/.cache/lia/collaboration.json")

CONFIDENCE_CERTA = 0.85      # undo without asking
CONFIDENCE_DUVIDA = 0.50     # below this, treat it as normal conversation
CONFIDENCE_STOP = 0.70

INTERVALO_NARRATION = 5.0    # minimum seconds between narrator messages

# ----------------------------------------------------------------------
# prompts
# ----------------------------------------------------------------------

SYSTEM_INTERPRETE = """You are Lia's co-work coordinator. The user is speaking to Lia (a voice assistant) while a coding agent (OpenCode) works in a project folder. You decide what the user's utterance MEANS and what Lia should do. Speech is transcribed, informal, may be in Portuguese, may contain jokes, filler and corrections.

Reply with ONE JSON object and nothing else:
{
 "acao": "instruir" | "parar" | "voltar" | "state" | "conversa" | "encerrar",
 "confianca": number 0..1 (how sure you are about the action),
 "prompt": string (only for "instruir"),
 "resumo": string (only for "instruir": max 8 words, English, what this step does),
 "steps": integer >= 1 (only for "voltar": how many instruction steps to undo, default 1),
 "pergunta": string (English, one short sentence Lia asks aloud when unsure; start by admitting the doubt, e.g. "I am not sure - do you want me to undo the last change?")
}

Actions:
- instruir: the user wants the coding agent to DO something (create, change, fix, run, explain code, investigate).
- parar: stop what the agent is doing right now, without undoing files.
- voltar: undo / go back to the previous version of the work ("volta", "desfaz isso", "não ficou bom, como estava antes").
- estado: the user asks what is happening / what was done so far.
- conversa: anything else - jokes, comments, thanks, questions unrelated to the code, thinking aloud. NOT sent to the agent.
- encerrar: the user ends the collaboration session.

Rules for "prompt" (you write it, the agent never hears the user's words):
- English, imperative, self-contained, concrete. State the goal, constraints and what to verify (run tests if it makes sense).
- Use the context (recent activity, files) to name files/functions instead of vague words like "that" or "it".
- Never copy the transcript literally; never include greetings or filler. Do not invent requirements the user did not express.
- If the utterance mixes a joke and a request, keep only the request.
- If the user corrected themselves, use the final version only.
Be conservative: when unsure between instruir and conversa, lower the confidence.

Confidence calibration (important, an undo deletes work):
- "voltar" with confianca >= 0.9 ONLY for an explicit, direct command to undo: "undo that", "go back", "revert it", "bring back the previous version", "desfaz isso", "volta".
- Opinions, hesitation or complaints without a clear command ("maybe that wasn't so good", "hmm", "I don't like it", "that's not right", "não ficou bom") are AMBIGUOUS about the agent's work: answer "voltar" with confianca 0.6-0.7 so Lia asks first. Never "conversa" for a complaint about the work done (use "instruir" only if the user says what to fix).
- Hedging words ("maybe", "I think", "not sure", "talvez") cap the confidence at 0.7.
- "parar" with confianca >= 0.8 only for an explicit "stop", "wait", "cancel", "para"."""

SYSTEM_CONFIRMATION = """The assistant just asked the user a yes/no question about undoing or stopping coding work. Decide what the user's reply means. Reply ONE JSON object: {"response": "sim" | "nao" | "outro"}. "outro" = the reply is not an answer (a new request or unrelated talk)."""

SYSTEM_NARRATOR = """You are Lia, a friendly voice assistant, narrating aloud what a coding agent just did. Write ONE very short spoken sentence in English (max 14 words), first person ("I created hello.py", "Running the tests now"). No markdown, no code, no file paths longer than the file name. If several things happened, mention the most important. If a command failed, say it failed."""

SYSTEM_SUMMARY = """You are Lia, a friendly voice assistant. The coding agent just finished. In English, speak 1-3 short sentences saying what was done and the result (tests passed/failed, files changed). No markdown, no code blocks, no long paths. If it asks the user something, repeat the question."""


# ----------------------------------------------------------------------
# Default LLM (Groq), used only outside tests
# ----------------------------------------------------------------------

def llm_default():
    """
    Devolve llm(sistema, usuario, narrador=False) usando o Groq da Lia.
    O narrador usa GROQ_API_KEY_NARRADOR (outra conta) quando existir,
    com o modelo LIA_NARRADOR_MODELO (padrão openai/gpt-oss-20b).
    """

    import groq
    import llm as llm_lia          # Lia module (GROQ_CLIENT)
    import config

    client_narrator = None
    key = os.environ.get("GROQ_API_KEY_NARRADOR")

    if key:
        client_narrator = groq.Groq(api_key=key)

    model_narrator = os.environ.get("LIA_NARRADOR_MODELO", "openai/gpt-oss-20b")

    def call(system, user, narrator=False):

        if narrator:
            client = client_narrator or llm_lia.GROQ_CLIENT
            model = model_narrator
        else:
            client = llm_lia.GROQ_CLIENT
            model = config.GROQ_MODEL

        parametros = dict(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            # gpt-oss reasoning uses these tokens: if there are too few, the
            # response will be empty.
            max_tokens=2000,
            extra_body={"reasoning_effort": "low"},
        )

        if "ONE JSON object" in system:
            parametros["response_format"] = {"type": "json_object"}

        response = client.chat.completions.create(**parametros)

        return response.choices[0].message.content or ""

    return call


def _json_of_response(text):
    """Extrai o primeiro objeto JSON de uma resposta de LLM (tolerante)."""

    if not text:
        return {}

    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()

    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except ValueError:
        pass

    ini, end = text.find("{"), text.rfind("}")

    if ini >= 0 and end > ini:
        try:
            value = json.loads(text[ini:end + 1])
            return value if isinstance(value, dict) else {}
        except ValueError:
            return {}

    return {}


# ----------------------------------------------------------------------
# restore points (git)
# ----------------------------------------------------------------------

class Checkpoints:
    """Um commit por instrução, num ramo exclusivo da Lia."""

    def __init__(self, folder):
        self.folder = folder
        self.branch_original = None

    def _git(self, *args, check=True):

        command = [
            "git", "-c", "user.name=Lia", "-c", "user.email=lia@localhost",
            "-c", "core.quotepath=off", *args,
        ]

        r = subprocess.run(
            command, cwd=self.folder, capture_output=True, text=True
        )

        if check and r.returncode != 0:
            raise OpenCodeError(f"git {' '.join(args)}: {r.stderr.strip()}")

        return r.stdout.strip()

    def ensure_repo(self):

        if not os.path.isdir(os.path.join(self.folder, ".git")):
            self._git("init", "-q")

        # HEAD has no commits: create an empty one to provide a restore point.
        if subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"],
            cwd=self.folder, capture_output=True
        ).returncode != 0:
            self._git("commit", "-q", "--allow-empty", "-m", "lia: início")

    def open_branch(self, name):
        """Vai para o ramo `nome` (cria se não existir). O que estava
        modificado vem junto e entra no primeiro ponto."""

        current = self._git("rev-parse", "--abbrev-ref", "HEAD")

        if current != name:

            self.branch_original = current

            exists = subprocess.run(
                ["git", "rev-parse", "--verify", "-q", f"refs/heads/{name}"],
                cwd=self.folder, capture_output=True
            ).returncode == 0

            self._git("checkout", "-q", name if exists else "-b",
                      *([] if exists else [name]))

    def checkpoint(self, label):
        """Grava tudo como um commit e devolve o hash curto."""

        self._git("add", "-A")
        self._git("commit", "-q", "--allow-empty", "--no-verify",
                  "-m", f"lia: {label}"[:200])

        return self._git("rev-parse", "--short", "HEAD")

    def changed_since(self, checkpoint):
        """Arquivos diferentes entre o ponto e o estado atual."""

        output = self._git("diff", "--name-status", checkpoint, check=False)
        extras = self._git("ls-files", "--others", "--exclude-standard",
                           check=False)

        lines = [l for l in output.splitlines() if l.strip()]
        lines += [f"A\t{a}" for a in extras.splitlines() if a.strip()]

        return lines

    def undo(self, checkpoint):
        """Descarta tudo depois do ponto (inclusive arquivos novos)."""

        antes = self.changed_since(checkpoint)

        self._git("reset", "-q", "--hard", checkpoint)
        self._git("clean", "-q", "-fd")

        return antes

    def diff(self, a, b=None):

        args = ["diff", "--stat", a] + ([b] if b else [])
        return self._git(*args, check=False)

    def diff_complete(self, a, b=None):

        args = ["diff", a] + ([b] if b else [])
        return self._git(*args, check=False)


# ----------------------------------------------------------------------
# narration: OpenCode event -> short sentence
# ----------------------------------------------------------------------

def _name(path):
    return os.path.basename(str(path)) if path else ""


def describe_part(part):
    """
    Transforma uma "parte" de ferramenta CONCLUÍDA (ou com erro) em um item
    do feed: {"type", "text", ...}. None se não for algo que valha mostrar.
    """

    if not isinstance(part, dict) or part.get("type") != "tool":
        return None

    state = part.get("state") or {}
    status = state.get("status")

    if status not in ("completed", "error"):
        return None

    tool = part.get("tool")
    input = state.get("input") or {}
    output = str(state.get("output") or state.get("error") or "")
    meta = state.get("metadata") or {}

    if tool in ("write", "edit", "patch", "multiedit"):

        path = input.get("filePath") or input.get("path") or ""

        return {
            "type": "arquivo",
            "acao": "criou" if tool == "write" else "editou",
            "arquivo": path,
            "text": ("Wrote " if tool == "write" else "Edited ")
                     + _name(path),
            "failure": status == "error",
        }

    if tool == "bash":

        output_cmd = str(meta.get("output") or output)
        codigo = meta.get("exit")

        return {
            "type": "command",
            "command": input.get("command", ""),
            "codigo": codigo,
            "saida": output_cmd[-1500:],
            "text": "Ran: " + str(input.get("command", ""))[:120],
            "failure": status == "error" or (codigo not in (None, 0)),
        }

    if tool in ("read", "list", "glob", "grep"):

        target = (input.get("filePath") or input.get("path")
                or input.get("pattern") or "")

        return {
            "type": "leitura",
            "text": f"Read {_name(target)}" if target else "Looked at the project",
            "failure": False,
        }

    return {
        "type": "outro",
        "text": f"Used {tool}",
        "failure": status == "error",
    }


# ----------------------------------------------------------------------
# collaboration
# ----------------------------------------------------------------------

class Collaboration:
    """
    Uma sessão de co-work numa pasta.

    Callbacks (todos opcionais, chamados em threads internas):
      ao_narrar(texto)        frase curta para a Lia FALAR enquanto trabalha
      ao_perguntar(texto)     pergunta de confirmação para a Lia falar
      ao_concluir(dict)       terminou: {"speech", "arquivos", "response", "ponto"}
      ao_permissao(dict)      OpenCode pediu permissão: {"id","session","title"}
      ao_feed(dict)           tudo que a ilha mostra (arquivo, comando, texto,
                              estado, ponto, erro)
    """

    def __init__(
        self, folder, llm, *, model=None, executable="opencode",
        ao_narrate=None, ao_ask=None, ao_concluir=None,
        ao_permission=None, ao_feed=None, server=None,
        file_state=FILE_STATE,
    ):
        self.folder = os.path.realpath(os.path.expanduser(folder))
        self.llm = llm
        self.model = model
        self.executable = executable

        self.ao_narrate = ao_narrate
        self.ao_ask = ao_ask
        self.ao_concluir = ao_concluir
        self.ao_permission = ao_permission
        self.ao_feed = ao_feed

        self.file_state = file_state

        self.server = server           # injectable (tests)
        self.client = None
        self.session = None
        self.checkpoints = Checkpoints(self.folder)

        self.history = []                # [{hash, label, time}]
        self.branch = None

        self.busy = False
        self._aborto = False
        self._pending = None              # pending confirmation
        self._warning = None                 # text to prepend to the next prompt
        self._rotulos = deque()            # sent instructions, in order
        self._label_current = None
        self._pai_current = None
        self._recent = deque(maxlen=12)  # feed items (Lia context)
        self._queue_narration = []
        self._last_narration = 0.0
        self._last_text = ""            # text the agent is writing
        self._files = []
        self._has_error = False
        self._lock = threading.RLock()
        self._lock_speech = threading.Lock()
        self._vistos = set()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------
    # open / close
    # ------------------------------------------------------------------

    def start(self, resume=True):
        """Sobe o OpenCode, o ramo git e a sessão (nova ou retomada)."""

        self.checkpoints.ensure_repo()

        saved = self._read_state().get(self.folder) if resume else None

        self.branch = (saved or {}).get("ramo") or f"lia/{time.strftime('%Y%m%d-%H%M%S')}"
        self.checkpoints.open_branch(self.branch)

        if self.server is None:
            self.server = OpenCodeServer(self.folder, executable=self.executable)

        self.server.start()
        self.client = self.server.client()

        # Resume the session if OpenCode still recognizes it.
        self.history = list((saved or {}).get("pontos") or [])
        session_id = (saved or {}).get("session")

        if session_id:
            try:
                conhecidas = {s.get("id") for s in (self.client.sessions() or [])}
            except OpenCodeError:
                conhecidas = set()

            if session_id not in conhecidas:
                session_id = None

        if not session_id:
            session_id = self.client.create_session("Lia co-work")

        self.session = session_id

        if not self.history:
            self.history.append(self._new_checkpoint("start of collaboration"))

        self._save_state()

        self.client.start_events()

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

        return {
            "retomada": bool(saved and saved.get("session") == self.session),
            "ramo": self.branch,
            "session": self.session,
        }

    def finish(self):
        """Fecha tudo: o servidor só existe durante a colaboração."""

        self._stop.set()

        try:
            if self.client:
                self.client.stop_events()
        except Exception:
            pass

        try:
            if self.server:
                self.server.stop()
        except Exception:
            pass

        self._save_state()

    # ------------------------------------------------------------------
    # saved state
    # ------------------------------------------------------------------

    def _read_state(self):

        try:
            with open(self.file_state, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self):

        all = self._read_state()

        all[self.folder] = {
            "session": self.session,
            "ramo": self.branch,
            "ramo_original": self.checkpoints.branch_original
                             or (all.get(self.folder) or {}).get("ramo_original"),
            "pontos": self.history,
        }

        try:
            os.makedirs(os.path.dirname(self.file_state), exist_ok=True)

            with open(self.file_state, "w", encoding="utf-8") as f:
                json.dump(all, f, ensure_ascii=False, indent=1)
        except OSError as e:
            print(f"[!] Não consegui salvar o estado da colaboração: {e}")

    def _new_checkpoint(self, label):

        return {
            "hash": self.checkpoints.checkpoint(label),
            "label": label,
            "t": time.time(),
        }

    # ------------------------------------------------------------------
    # feed / context
    # ------------------------------------------------------------------

    def _feed(self, item):

        item.setdefault("t", time.time())
        self._recent.append(item)

        if self.ao_feed:
            try:
                self.ao_feed(item)
            except Exception as e:
                print(f"[!] ao_feed: {e}")

    def _context(self):
        """Resumo do que está acontecendo, para o interpretador."""

        lines = [f"Project folder: {self.folder}",
                  f"Agent is currently: {'working' if self.busy else 'idle'}"]

        if self.history:
            lines.append("Instruction steps so far (oldest first): " +
                          "; ".join(p["label"] for p in self.history[-6:]))

        if self._recent:
            lines.append("Recent agent activity: " +
                          "; ".join(i.get("text", "") for i in list(self._recent)[-8:]))

        if self._last_text:
            lines.append(f"Agent's last message: {self._last_text[:300]}")

        return "\n".join(lines)

    def state_speakable(self):
        """Frase curta (até ~15 s falada) sobre o andamento. Nunca lê comandos."""

        items = list(self._recent)

        files = []
        comandos = falhas = 0

        for i in items:
            if i.get("type") == "arquivo" and i.get("arquivo"):
                name = _name(i["arquivo"])
                if name not in files:
                    files.append(name)
            elif i.get("type") == "command":
                comandos += 1
                falhas += 1 if i.get("failure") else 0

        if not files and not comandos:
            return ("I'm just getting started." if self.busy
                    else "I haven't done anything in this session yet.")

        parts = []

        if files:
            names = ", ".join(files[:3]) + (" and more" if len(files) > 3 else "")
            parts.append(f"touched {names}")

        if comandos:
            parts.append(
                f"ran {comandos} command{'s' if comandos > 1 else ''}"
                + (f", {falhas} failed" if falhas else "")
            )

        summary = " and ".join(parts)

        if self.busy:
            return f"I'm still working. So far I {summary}."

        return f"I'm idle. Last time I {summary}."

    # ------------------------------------------------------------------
    # user speech
    # ------------------------------------------------------------------

    def process_speech(self, speech):
        """
        Ponto de entrada da voz. Devolve um dict:
          {"acao": ..., "speech": texto para a Lia dizer (ou None)}
        `acao == "conversa"` significa: não é para o OpenCode; a Lia responde
        como sempre.
        """

        # A dedicated voice lock: NEVER use the event `_lock`, because undo()
        # waits for the event loop, which needs that lock.
        with self._lock_speech:

            if self._pending:

                response = self._read_confirmation(speech)

                if response == "sim":
                    pending, self._pending = self._pending, None
                    return self._execute(pending["acao"], pending)

                if response == "nao":
                    self._pending = None
                    return {"acao": "nenhuma", "speech": "Okay, I left it as it is."}

                self._pending = None      # it was something else: continue normally

            decision = self._interpret(speech)

            action = decision.get("acao")

            if not action:
                # Never pass this to Lia’s normal flow: it would run the
                # task on its own, outside co-work.
                return {"acao": "failure",
                        "speech": "Sorry, I didn't catch that. Could you say it again?"}
            confidence = float(decision.get("confianca") or 0)

            if action == "voltar":

                if confidence >= CONFIDENCE_CERTA:
                    return self._execute("voltar", decision)

                if confidence >= CONFIDENCE_DUVIDA:
                    return self._ask("voltar", decision)

                return {"acao": "conversa", "speech": None}

            if action == "parar":

                if confidence >= CONFIDENCE_STOP:
                    return self._execute("parar", decision)

                if confidence >= CONFIDENCE_DUVIDA:
                    return self._ask("parar", decision)

                return {"acao": "conversa", "speech": None}

            if action == "instruir":

                if confidence >= CONFIDENCE_DUVIDA and decision.get("prompt"):
                    return self._execute("instruir", decision)

                return {"acao": "conversa", "speech": None}

            if action == "state":
                return {"acao": "state", "speech": self.state_speakable()}

            if action == "encerrar":
                return {"acao": "encerrar", "speech": None}

            return {"acao": "conversa", "speech": None}

    def _interpret(self, speech):

        user = f"CONTEXT:\n{self._context()}\n\nUSER SAID (transcript):\n{speech}"

        for tentativa in (1, 2):

            try:
                decision = _json_of_response(self.llm(SYSTEM_INTERPRETE, user))
            except Exception as e:
                print(f"[!] Co-work: interpretação falhou ({tentativa}/2): {e}")
                decision = {}

            if decision.get("acao"):
                print(f"[co-work] \"{speech}\" -> {decision.get('acao')} "
                      f"(confiança {decision.get('confianca')})")
                return decision

        return {}

    def _read_confirmation(self, speech):

        question = self._pending.get("pergunta", "")

        try:
            raw = self.llm(
                SYSTEM_CONFIRMATION,
                f"Question asked: {question}\nUser reply: {speech}"
            )
        except Exception:
            return "outro"

        return _json_of_response(raw).get("response", "outro")

    def _ask(self, action, decision):

        if action == "voltar":
            steps = max(1, int(decision.get("steps") or 1))
            default = ("I'm not sure what you meant. Do you want me to undo "
                      "the last change?" if steps == 1
                      else f"I'm not sure what you meant. Do you want me to "
                           f"undo the last {steps} steps?")
        else:
            default = "I'm not sure what you meant. Do you want me to stop what the agent is doing?"

        question = decision.get("pergunta") or default

        self._pending = {**decision, "acao": action, "pergunta": question}

        if self.ao_ask:
            self.ao_ask(question)

        return {"acao": "perguntar", "speech": question}

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def _execute(self, action, decision):

        if action == "instruir":

            self.instruct(decision["prompt"], decision.get("resumo") or "instruction")

            return {"acao": "instruir", "speech": None}

        if action == "parar":

            self.stop_now()

            return {"acao": "parar", "speech": "Stopped."}

        if action == "voltar":

            return {"acao": "voltar",
                    "speech": self.undo(int(decision.get("steps") or 1))}

        return {"acao": "nenhuma", "speech": None}

    def instruct(self, prompt, label="instruction"):
        """Manda o prompt (já escrito pela Lia). Entra na fila se ocupado."""

        with self._lock:

            if self._warning:
                prompt = self._warning + "\n\n" + prompt
                self._warning = None

            self._rotulos.append(label)

            if not self.busy:
                self._files = []
                self._has_error = False
                self._last_text = ""

            self.busy = True

            self._feed({"type": "instrucao", "text": label, "prompt": prompt})

            self.client.send(self.session, prompt, model=self.model)

    def stop_now(self):
        """Interrompe o agente (não desfaz arquivos)."""

        with self._lock:
            self._aborto = True
            self._rotulos.clear()

        try:
            self.client.abort(self.session)
        except OpenCodeError as e:
            print(f"[!] Falha ao abortar: {e}")

        self._wait_idle(8)

    def undo(self, steps=1):
        """
        Para o agente, volta `passos` instruções e avisa o OpenCode (na
        próxima mensagem) que os arquivos mudaram por fora.
        """

        with self._lock:

            # The starting checkpoint always remains.
            target_indice = len(self.history) - 1 - steps

            if target_indice < 0:
                return "There is no earlier version to go back to."

            if self.busy:
                self._aborto = True
                self._rotulos.clear()

        if self.busy:

            try:
                self.client.abort(self.session)
            except OpenCodeError:
                pass

            self._wait_idle(8)

        with self._lock:

            target = self.history[target_indice]
            desfeitos = self.history[target_indice + 1:]

            alterados = self.checkpoints.undo(target["hash"])

            self.history = self.history[:target_indice + 1]
            self._save_state()

            names = ", ".join(p["label"] for p in desfeitos)

            self._warning = (
                "NOTE: the user reverted the project on disk to an earlier "
                f"version (undid: {names}). Files may have changed or been "
                "removed outside of your control. Re-read any file before "
                "editing it and do not assume earlier changes still exist."
            )

            self._feed({
                "type": "volta", "text": f"Went back to: {target['label']}",
                "ponto": target["hash"], "desfeitos": [p["label"] for p in desfeitos],
            })

        return (f"Done, I went back to before: {desfeitos[0]['label']}."
                if desfeitos else "Done, I went back.")

    def respond_permission(self, permission_id, response="once"):
        """resposta: once | always | reject"""

        self.client.respond_permission(self.session, permission_id, response)

    def diff(self, since=None):
        """Diff (git) do trabalho; por padrão desde o ponto anterior ao último."""

        if since is None:
            since = self.history[-2]["hash"] if len(self.history) > 1 \
                else self.history[0]["hash"]

        return self.checkpoints.diff_complete(since)

    def _wait_idle(self, limit):

        end = time.time() + limit

        while self.busy and time.time() < end:
            time.sleep(0.1)

    # ------------------------------------------------------------------
    # OpenCode events
    # ------------------------------------------------------------------

    def _loop(self):

        while not self._stop.is_set():

            try:
                ev = self.client.events.get(timeout=0.5)
            except queue.Empty:
                self._narrate_if_gives_time()
                continue

            try:
                self._handle(ev)
            except Exception as e:
                print(f"[!] Erro tratando evento do OpenCode: {e}")

    def _handle(self, ev):

        session = session_of_event(ev)

        if session and session != self.session:
            return

        tipo = event_type(ev)
        props = ev.get("properties") or {}

        permission = event_permission(ev)

        if permission:
            self._feed({"type": "permissao", "text": permission["title"] or "Permission",
                        "id": permission["id"]})

            if self.ao_permission:
                self.ao_permission(permission)

            return

        if tipo == "session.status" and not event_idle(ev):
            self.busy = True
            return

        if tipo == "message.updated":
            self._on_message(props.get("info") or {})
            return

        if tipo == "message.part.updated":
            self._on_part(props.get("part") or {})
            return

        if tipo == "message.part.delta" and props.get("field") == "text":
            self._last_text = (self._last_text + props.get("delta", ""))[-600:]
            return

        if tipo == "session.error":

            error = props.get("error") or {}

            if error.get("name") == "MessageAbortedError":
                self._aborto = True
                return

            msg = (error.get("data") or {}).get("message") or error.get("name") or "error"
            self._has_error = True
            self._feed({"type": "error", "text": str(msg)[:300]})
            self._narrate_now(f"The agent hit an error: {str(msg)[:80]}")
            return

        if event_idle(ev):
            self._on_idle()

    def _on_message(self, info):
        """Nova mensagem do agente respondendo a OUTRA instrução = a anterior terminou."""

        if info.get("role") != "assistant":
            return

        pai = info.get("parentID")

        if not pai or pai == self._pai_current:
            return

        with self._lock:

            previous = self._pai_current
            self._pai_current = pai

            if previous is not None and self._label_current is not None:
                self._close_instruction(self._label_current)

            self._label_current = self._rotulos.popleft() if self._rotulos else "instruction"

    def _close_instruction(self, label):
        """Cria o ponto de volta da instrução que acabou."""

        try:
            checkpoint = self._new_checkpoint(label)
        except OpenCodeError as e:
            print(f"[!] Não consegui criar o ponto de volta: {e}")
            return None

        self.history.append(checkpoint)
        self._save_state()

        self._feed({"type": "ponto", "text": f"Checkpoint saved: {label}",
                    "ponto": checkpoint["hash"]})

        return checkpoint

    def _on_part(self, part):

        if part.get("type") == "tool":
            state = part.get("state") or {}
            if state.get("status") == "running":
                self.busy = True

        item = describe_part(part)

        if not item:
            return

        key = part.get("callID") or part.get("id")

        if key in self._vistos:
            return

        self._vistos.add(key)

        if item["type"] == "arquivo" and item.get("arquivo"):

            if item["arquivo"] not in self._files:
                self._files.append(item["arquivo"])

        if item.get("failure"):
            self._has_error = True

        self._feed(item)

        if item["type"] in ("arquivo", "command"):
            self._queue_narration.append(item)
            self._narrate_if_gives_time()

    # ------------------------------------------------------------------
    # narration
    # ------------------------------------------------------------------

    def _narrate_if_gives_time(self):

        if not self._queue_narration:
            return

        if time.time() - self._last_narration < INTERVALO_NARRATION:
            return

        items, self._queue_narration = self._queue_narration, []

        summary = "; ".join(
            (i["text"] + (" (FALHOU)" if i.get("failure") else "")) for i in items
        )

        self._narrate_now(summary, raw=True)

    def _narrate_now(self, text, raw=False):

        if not self.busy:
            return                        # already finished; the final summary covers it

        self._last_narration = time.time()

        phrase = text

        if raw:

            try:
                phrase = self.llm(SYSTEM_NARRATOR, text, narrator=True).strip()
            except Exception as e:
                print(f"[!] Narrador falhou: {e}")
                phrase = ""

            if not phrase:
                return

        if self.ao_narrate:
            self.ao_narrate(phrase)

    # ------------------------------------------------------------------
    # end of work
    # ------------------------------------------------------------------

    def _on_idle(self):

        with self._lock:

            if not self.busy and not self._aborto:
                return          # repeated idle event

            abortado = self._aborto

            if self._label_current is not None and not abortado:
                self._close_instruction(self._label_current)

            elif self._label_current is not None and abortado and self._files:
                # stopped midway: save the current state so it can still be undone
                self._close_instruction(self._label_current + " (interrupted)")

            self._label_current = None
            self._pai_current = None
            self._rotulos.clear()
            self._queue_narration = []      # old narration should not be spoken after the session ends

            self.busy = False
            self._aborto = False

            files = list(self._files)

            self._feed({"type": "state", "text": "idle"})

        if abortado:
            return

        self._speak_completion(files)

    def _speak_completion(self, files):

        try:
            response = self.client.last_response(self.session) or self._last_text
        except OpenCodeError:
            response = self._last_text

        response = str(response or "")

        try:
            speech = self.llm(
                SYSTEM_SUMMARY,
                f"Files changed: {[_name(a) for a in files]}\n"
                f"Had errors: {self._has_error}\n"
                f"Agent's final message:\n{response[:1500]}",
                narrator=True,
            ).strip()
        except Exception as e:
            print(f"[!] Resumo falhou: {e}")
            speech = "I'm done."

        if self.ao_concluir:
            self.ao_concluir({
                "speech": speech or "I'm done.",
                "arquivos": files,
                "response": response,
                "ponto": self.history[-1]["hash"] if self.history else None,
            })
