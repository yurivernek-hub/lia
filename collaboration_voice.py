"""
collaboration_voice.py - liga o modo co-work (collaboration.py) à voz da Lia.

No jarvisb.py basta:

    colab = collaboration_voice.PonteColaboracao(voz)
    ...
    response = colab.handle(command_text)      # None = not handled here
    if resposta is None:
        resposta = gerente.responder_sobre_tarefas(comando_texto)

Ciclo:
  1. "let's work on the <project> project" / "start co-work" abre a sessão
     (sobe o OpenCode só agora, numa thread - a Lia continua ouvindo);
  2. com a sessão aberta, TODA speech passa antes pelo interpretador
     (collaboration.Colaboracao.processar_speech): instrução, parar, voltar,
     estado, encerrar... ou "conversa", que volta pro fluxo normal da Lia;
  3. narração, conclusão e perguntas entram na fila da ControleDeVoz;
  4. cada item do feed vai para o barramento como event "colab"
     (a ilha vai consumir isso).
"""

import json
import os
import re
import threading

from memory import address_suffix
from events import emit

import collaboration

# Cheap trigger before spending an LLM call to determine whether this means "open".
GATILHO_OPEN = re.compile(
    r"co-?work|collaborat|pair[- ]program|\bprojects?\b|open ?code|open code"
    r"|code together|let'?s (?:code|program|build|work)|work(?:ing)? on"
    r"|colabora|programar junto|projeto|vamos programar|trabalhar"
    r"|re-?open|resume|continue (?:the |our )?(?:work|session|code)|code work|coding",
    re.IGNORECASE,
)

SYSTEM_OPEN = """The user speaks to a voice assistant. Decide if they are asking to START a coding collaboration session (e.g. "let's work on the X project", "open co-work on X", "work together on X"; speech-to-text may split or misspell names, such as "open code workspace" for opencode_workspace) (a coding agent works on a project folder together with the assistant). Reply ONE JSON object: {"abrir": true|false, "projeto": string|null}. "projeto" = the project/folder name or path they mentioned, exactly as a folder name (no articles), or null if they named none. A joke, a past/hypothetical mention or a question about co-work is NOT a request: abrir=false."""

FOLDERS_DEFAULT = ["~", "~/projetos", "~/projects", "~/dev", "~/Documents", "~/Documentos"]

YES = re.compile(r"\b(yes|yeah|yep|sure|ok(?:ay)?|go ahead|allow|do it|please do|sim|pode|claro|permite|autorizo|isso)\b", re.I)
NOT = re.compile(r"\b(no|nope|don'?t|deny|reject|cancel|n[aã]o|nega|negado)\b", re.I)
SEMPRE = re.compile(r"\b(always|every ?time|sempre)\b", re.I)


def _normalize(text):

    import unicodedata

    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()

    return re.sub(r"[^a-z0-9]+", "", text.lower())


class CollaborationBridge:

    def __init__(self, voice, llm_func=None, folders_base=None):

        self.voice = voice
        self._llm = llm_func
        self.col = None
        self.active = False
        self.opening = False

        self._waiting_folder = False
        self._permission = None
        self._lock = threading.Lock()

        bases = os.environ.get("LIA_PROJETOS")
        self.folders_base = (
            folders_base or (bases.split(":") if bases else FOLDERS_DEFAULT)
        )

    # ------------------------------------------------------------------
    # LLM (created only when needed)
    # ------------------------------------------------------------------

    def llm(self):

        if self._llm is None:
            self._llm = collaboration.llm_default()

        return self._llm

    # ------------------------------------------------------------------
    # input: called for EVERY user utterance
    # ------------------------------------------------------------------

    def handle(self, text):
        """
        Devolve o que a Lia deve dizer, ou None para a speech seguir o fluxo
        normal (conversa comum, clima, tarefas...).
        """

        if self.opening:

            # While it starts, nothing goes to the normal flow (it would run the task
            # on its own in the background, outside co-work).
            return f"I'm still starting the co-work session{address_suffix()}. One moment."

        if self.active:
            return self._handle_active(text)

        if self._waiting_folder:
            return self._resolve_folder_spoken(text)

        if not GATILHO_OPEN.search(text):
            return None

        print(f"[co-work] possível pedido de abrir: \"{text}\"")

        try:
            request = collaboration._json_of_response(
                self.llm()(SYSTEM_OPEN, text)
            )
        except Exception as e:
            print(f"[!] Co-work: não consegui interpretar o pedido: {e}")
            return None

        print(f"[co-work] pedido de abrir? \"{text}\" -> {request}")

        if not request.get("abrir"):
            return None

        project = request.get("projeto")

        if not project:

            self._waiting_folder = True

            return f"Which project folder{address_suffix()}?"

        return self._open_por_name(project)

    def _handle_active(self, text):

        # Response to an OpenCode permission request.
        if self._permission:

            response = self._read_permission(text)

            if response:

                request, self._permission = self._permission, None

                try:
                    self.col.respond_permission(request["id"], response)
                except Exception as e:
                    return f"I couldn't answer the permission request: {e}"

                return "Allowed." if response != "reject" else "Denied."

        busy_antes = self.col.busy

        r = self.col.process_speech(text)

        action = r.get("acao")

        if action == "instruir":
            return ("Added to the queue." if busy_antes
                    else f"On it{address_suffix()}.")

        if action == "encerrar":
            return self.close()

        if action == "conversa":
            return None

        return r.get("speech")

    @staticmethod
    def _read_permission(text):

        if NOT.search(text):
            return "reject"

        if YES.search(text):
            return "always" if SEMPRE.search(text) else "once"

        return None

    # ------------------------------------------------------------------
    # open / close
    # ------------------------------------------------------------------

    def _resolve_folder_spoken(self, text):

        self._waiting_folder = False

        return self._open_por_name(text)

    def _open_por_name(self, name):

        folder = self.achar_folder(name)

        if not folder:
            return (f"I couldn't find a folder called {name}{address_suffix()}. "
                    "Tell me the project name again, or put it under your "
                    "projects folder.")

        return self.open(folder)

    def achar_folder(self, name):
        """Caminho absoluto, ou a pasta cujo nome mais se parece com `nome`
        (até 2 níveis dentro das pastas-base)."""

        name = str(name).strip()

        path = os.path.expanduser(name)

        if os.path.isabs(path) and os.path.isdir(path):
            return path

        target = _normalize(re.sub(r"\bproject\b|\bfolder\b", "", name, flags=re.I))

        if not target:
            return None

        melhor = None

        for base in self.folders_base:

            base = os.path.expanduser(base)

            if not os.path.isdir(base):
                continue

            try:
                level1 = [d for d in os.scandir(base) if d.is_dir() and not d.name.startswith(".")]
            except OSError:
                continue

            candidatos = list(level1)

            for d in level1:
                try:
                    candidatos += [x for x in os.scandir(d.path)
                                   if x.is_dir() and not x.name.startswith(".")]
                except OSError:
                    pass

            for d in candidatos:

                n = _normalize(d.name)

                if n == target:
                    return d.path

                if target in n or (len(n) >= 4 and n in target):
                    melhor = melhor or d.path

        return melhor

    def open(self, folder):
        """Sobe a sessão em segundo plano; avisa por voz quando estiver pronta."""

        with self._lock:

            if self.active or self.opening:
                return f"A session is already open{address_suffix()}."

            self.opening = True

        name = os.path.basename(folder.rstrip("/")) or folder

        threading.Thread(
            target=self._open_thread, args=(folder, name), daemon=True
        ).start()

        return (f"Opening the co-work session on {name}. "
                "I'll tell you when it's ready.")

    def _open_thread(self, folder, name):

        try:

            self.col = collaboration.Collaboration(
                folder,
                self.llm(),
                ao_narrate=self._ao_narrate,
                ao_concluir=self._ao_concluir,
                ao_permission=self._ao_permission,
                ao_feed=self._on_feed,
            )

            info = self.col.start()

            self.active = True

            emit("colab", item={"type": "session", "state": "aberta",
                                  "pasta": folder, "ramo": info["ramo"],
                                  "retomada": info["retomada"]})

            self.voice.announce(
                (f"Co-work is back on {name}, continuing where we left off."
                 if info["retomada"] else
                 f"Co-work is ready on {name}. Tell me what to build."),
                origin="colab",
            )

        except Exception as e:

            print(f"[!] Co-work: failed ao abrir: {e}")

            self.col = None

            self.voice.announce(
                f"I couldn't start the co-work session{address_suffix()}. "
                "Check that OpenCode is installed.",
                origin="colab",
            )

        finally:
            self.opening = False

    def close(self):

        if not self.active or not self.col:
            return None

        col, self.col = self.col, None
        self.active = False
        self._permission = None

        branch = col.branch

        threading.Thread(target=col.finish, daemon=True).start()

        emit("colab", item={"type": "session", "state": "fechada"})

        return (f"Co-work closed{address_suffix()}. Everything is saved on the "
                f"branch {branch}.")

    def close_silent(self):

        try:
            if self.col:
                self.col.finish()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # OpenCode callbacks (run in internal threads)
    # ------------------------------------------------------------------

    def _ao_narrate(self, text):
        self.voice.announce(text, origin="colab")

    def _ao_concluir(self, data):
        self.voice.announce(data["speech"], origin="colab")

    def _ao_permission(self, p):

        self._permission = p

        title = p.get("title") or "to do something"

        self.voice.announce(
            f"OpenCode is asking permission {title}. Should I allow it?",
            origin="colab",
        )

    def _on_feed(self, item):

        try:
            emit("colab", item=json.loads(json.dumps(item, default=str)))
        except Exception as e:
            print(f"[!] Co-work: erro ao emitir feed: {e}")
