"""
Tarefas em segundo plano da Lia.

Toda task com tools roda numa THREAD própria (a mesma Tarefa do
MVP 1, task.py). O que muda é quem espera por ela:

  - "Anexada": a thread principal espera, atendendo confirmações (voz ou
    botão), "stop" digitado e a interface, como no MVP 1. Se passar de
    PRIMEIRO_PLANO_SEGUNDOS, ela é DESANEXADA: a Lia avisa que vai
    continuar em segundo plano e o loop volta a ouvir a wake word.
  - Em segundo plano (pedido explícito, "in the background..."): nasce
    desattached. A thread principal segue a vida normal.

Uma task desattached nunca speech, nunca escuta e nunca mexe no estado da
interface. Ela só:
  - termina e entra na fila do ControleDeVoz como mensagem de resultado
    (a Lia speech quando o usuário estiver livre);
  - precisa de confirmação (sudo) e entra na fila como mensagem
    interativa (a Lia pergunta no momento certo; sem resposta, cancela);
  - emite events "task" pra interface (id, estado, passo).

Ferramentas que mexem na tela (clima, notícias, abrir app) ficam
bloqueadas depois que a task vai pro segundo plano.
"""

import itertools
import re
import threading
import time
from dataclasses import dataclass, field

from memory import address_suffix
import config
import llm
from confirmation import VoiceConfirmer
from voice_control import PRIORIDADE_CONFIRMATION,PRIORIDADE_RESULT
from events import emit as _emit_barramento
from task import (
    Task,
    ResultTask,
    Confirmer,
    YES,
    NOT,
    WITHOUT_RESPONSE,
)
from utils import normalize_text
import task_memory
import personality

# Tunable values from config.py; these are used if a setting is absent.
FIRST_PLAN_SECONDS = getattr(config,"TAREFA_PRIMEIRO_PLANO_SEGUNDOS",15)
MAX_SECOND_PLAN = getattr(config,"TAREFA_MAX_SEGUNDO_PLANO",2)
# 0 = no time limit: the task runs until it finishes or is cancelled.
BG_LIMIT_SECONDS = 0
BG_MAX_STEPS = getattr(config,"TAREFA_BG_MAX_PASSOS",30)
WAIT_CONFIRMATION = getattr(config,"TAREFA_CONFIRMACAO_ESPERA_SEGUNDOS",600)

TOOLS_OF_SCREEN = {"mostrar_clima","mostrar_noticias","abrir_aplicativo"}

# Tools that modify the computer (run commands, create/edit
# files). Only tasks that use one of these get a task graph
# and can be sent to the background. Questions (weather, news, search,
# schedule, reminders, etc.) are answered directly without a graph.
TOOLS_TECNICAS = {"executar_comando","criar_arquivo","editar_arquivo"}

TOOLS_SECOND_PLAN = {
    "criar_arquivo","ler_arquivo","listar_diretorio","editar_arquivo",
    "executar_comando","pesquisar_web","consultar_calendarios",
    "consultar_tarefas_recentes","criar_lembrete","listar_reminders",
    "cancelar_lembrete",
}

# ----------------------------------------------------------------------
# voice intents (in English, because Lia’s STT uses English)
# ----------------------------------------------------------------------

DEFAULT_SECOND_PLAN = re.compile(
    r"\bin the background\b"
    r"|\bbackground (?:task|job)\b"
    r"|\b(?:let|tell) me know when (?:it(?:'s| is)|you(?:'re| are)|that(?:'s| is)) (?:done|finished|ready)\b"
    r"|\btell me when (?:it(?:'s| is)|you(?:'re| are)|that(?:'s| is)) (?:done|finished|ready)\b"
    r"|\bnotify me when\b",
    re.IGNORECASE
)

_DEFAULT_STATUS = re.compile(
    r"\b(?:what(?:'s| is| are) you (?:working on|doing)"
    r"|are you (?:done|finished)(?: yet)?"
    r"|how(?:'s| is) (?:the |that )?(?:background )?(?:task|job)(?: going)?"
    r"|how(?:'s| is) (?:it|that|everything|this) going"
    r"|(?:background )?(?:task|job) status"
    r"|status of (?:the |that )?(?:background )?(?:task|job)"
    r"|any (?:progress|news) on)\b",
    re.IGNORECASE
)

_DEFAULT_CANCEL = re.compile(
    r"\b(?:cancel|stop|abort|kill)\b.*\b(?:background|task|job)\b",
    re.IGNORECASE
)

_MENCIONA_TASK = re.compile(r"\b(?:task|job|background)\b",re.IGNORECASE)


def request_second_plan(text):
    """O usuário pediu explicitamente pra fazer em segundo plano?"""

    return bool(DEFAULT_SECOND_PLAN.search(text or ""))


def title_short(request):
    """Até 5 palavras do pedido, sem a frase de segundo plano (para o indicador)."""

    text = DEFAULT_SECOND_PLAN.sub(" ",request or "")
    text = re.sub(r"\bplease\b"," ",text,flags=re.IGNORECASE)
    words = re.findall(r"[\w'+\-\.]+",text)

    return " ".join(words[:5]) or "task"


def ensure_sources(text,result):
    """
    Se a resposta falada não citou nenhuma das fontes da pesquisa,
    acrescenta "Sources: A and B." no final (pelo nome do site, nunca
    o endereço).
    """

    names = (result.get("fontes") or [])[:2]

    if not names or not text:
        return text

    spoken = normalize_text(text)

    if any(normalize_text(n) in spoken for n in names):
        return text

    label = "Source" if len(names) == 1 else "Sources"

    return f"{text} {label}: {' and '.join(names)}."


# ----------------------------------------------------------------------


@dataclass
class RequestConfirmation:
    tool: str
    description: str
    args: dict
    event: threading.Event = field(default_factory=threading.Event)
    decision: str = None
    served: bool = False
    expired: bool = False


class ConfirmadorBridge(Confirmer):
    """Confirmador da thread da task: pede à thread principal."""

    def __init__(self,running_task):
        self.te = running_task

    def confirm(self,tool,description,args):
        return self.te.request_confirmation(tool,description,args)


class RunningTask:

    def __init__(self,manager,id,request,context,attached):

        self.manager = manager
        self.id = id
        self.request = request
        self.context = context
        self.attached = attached
        self.nasceu_em_second_plan = not attached

        self.lock = threading.RLock()
        self.finished = threading.Event()
        self.cancel_ev = threading.Event()
        self.result = None

        self.n_steps = 0
        self.step_current = ""
        self.first_confirm = False

        self.request_conf = None
        self.last_decision = None
        self.cancelled_by_user = False

        self.criada = time.time()
        self.start = time.time()
        self.steps_info = []       # [{n, description, tool, success}]
        self.usou_command = False   # has any technical tool been used?

    # --- events for the interface ---

    def progress(self):
        """Estado atual para o status falado e para o indicador."""

        return {
            "steps": len(self.steps_info),
            "atual": self.step_current,
            "decorrido_min": max(0,int((time.time() - self.start) // 60)),
            "ultimo_ok": next(
                (p["description"] for p in reversed(self.steps_info)
                 if p.get("success") is True),
                ""
            ),
        }

    def emit(self,type,**data):
        """
        Eventos para a interface. "task" vai SEMPRE (o indicador mostra
        as tarefas, attacheds ou não). "state" (a esfera) só enquanto a
        task é attached: em segundo plano a Lia não mexe no estado da UI.
        """

        if type == "passo":

            self.step_current = data.get("description") or ""

            if data.get("ferramenta") in TOOLS_TECNICAS:
                self.usou_command = True

            self.steps_info.append({
                "n": data.get("n"),
                "description": data.get("description") or "",
                "ferramenta": data.get("ferramenta") or "",
                "success": None,
            })

            _emit_barramento(
                "task",
                id=self.id,
                state="step",
                n=data.get("n"),
                description=data.get("description") or "",
                tool=data.get("ferramenta") or "",
            )

        elif type == "result":

            if self.steps_info:
                self.steps_info[-1]["success"] = bool(data.get("success"))

            _emit_barramento(
                "task",
                id=self.id,
                state="result",
                success=bool(data.get("success")),
            )

        elif type == "state" and self.attached:

            _emit_barramento("state",**data)

    def block(self,name):
        return (not self.attached) and name in TOOLS_OF_SCREEN

    def register_step(self,n,name,args,description,confirm):

        self.n_steps = n
        self.step_current = description

        if n == 1:
            self.first_confirm = confirm

    def cancel(self,by_user=True):

        if by_user:
            self.cancelled_by_user = True

        self.cancel_ev.set()

    # --- confirmation (task thread side) ---

    def request_confirmation(self,tool,description,args):

        request = RequestConfirmation(tool,description,args)

        with self.lock:

            via_queue = not self.attached

            if not via_queue:
                self.request_conf = request

        if via_queue:
            self.manager._enqueue_confirmation(self,request)

        limit = time.time() + WAIT_CONFIRMATION

        while not request.event.wait(0.2):

            if self.cancel_ev.is_set():
                request.decision = NOT
                break

            if time.time() > limit:
                request.decision = WITHOUT_RESPONSE
                request.expired = True
                break

        with self.lock:

            if self.request_conf is request:
                self.request_conf = None

        self.last_decision = request.decision or NOT

        return self.last_decision

    # --- confirmation (main thread side, attached) ---

    def request_pending(self):
        """Pedido de confirmação ainda não atendido (uma vez só)."""

        with self.lock:

            request = self.request_conf

            if request and not request.served and not request.event.is_set():
                request.served = True
                return request

        return None

    @staticmethod
    def respond(request,decision):

        request.decision = decision
        request.event.set()

    # --- task completion ---

    def finalize(self,result):

        with self.lock:
            self.result = result
            attached = self.attached
            self.finished.set()

        if not attached:
            self.manager._announce_result(self)


class ManagerOfTasks:

    def __init__(
        self,
        control=None,
        first_plan=FIRST_PLAN_SECONDS,
        max_simultaneas=MAX_SECOND_PLAN,
    ):

        self.control = control
        self.first_plan = first_plan if control is not None else 0
        self.max_simultaneas = max_simultaneas

        self._tasks = []
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def active(self):
        """Tarefas em segundo plano ainda rodando."""

        with self._lock:

            return [
                t for t in self._tasks
                if not t.attached and not t.finished.is_set()
            ]

    def start(self,request,context,second_plan=False):
        """Começa a task. None = limite de tarefas em segundo plano."""

        with self._lock:

            em_bg = [
                t for t in self._tasks
                if not t.attached and not t.finished.is_set()
            ]

            if second_plan and len(em_bg) >= self.max_simultaneas:
                return None

            te = RunningTask(
                self,
                next(self._seq),
                request,
                context,
                attached=not second_plan
            )

            self._tasks.append(te)
            self._tasks = self._tasks[-20:]

        threading.Thread(
            target=self._run,
            args=(te,),
            daemon=True
        ).start()

        _emit_barramento(
            "task",
            id=te.id,
            state="started",
            title=title_short(request),
            second_plan=second_plan,
        )

        return te

    def detach(self,te):
        """
        Manda uma task em primeiro plano pro segundo plano. False se
        não der: já terminou, está esperando confirmação, já há tarefas
        demais em segundo plano, ou é só uma pergunta (nunca rodou comando
        nem mexeu em arquivo): pergunta lenta continua em primeiro plano
        até responder, sem gráfico e sem virar task de background.
        """

        if self.control is None:
            return False

        if not te.usou_command:
            return False

        if len(self.active()) >= self.max_simultaneas:
            return False

        with te.lock:

            if te.finished.is_set() or te.request_conf is not None:
                return False

            te.attached = False

        _emit_barramento(
            "task",
            id=te.id,
            state="started",
            title=title_short(te.request),
            second_plan=True,
        )

        return True

    # ------------------------------------------------------------------

    def _run(self,te):

        longa = te.nasceu_em_second_plan or self.first_plan > 0

        try:

            task = Task(
                te.request,
                context=te.context,
                confirmador=ConfirmadorBridge(te),
                cancelled=te.cancel_ev.is_set,
                ao_step=te.register_step,
                emit=te.emit,
                tools=(
                    TOOLS_SECOND_PLAN
                    if te.nasceu_em_second_plan else None
                ),
                task_id=te.id,
                max_steps=BG_MAX_STEPS if longa else None,
                limit_seconds=BG_LIMIT_SECONDS if longa else None,
                bloquear=te.block
            )

            result = task.execute()

        except Exception as e:

            print(f"\n[!] Erro inesperado na task {te.id}: {e}")

            result = ResultTask(
                state="error",
                response="I hit an unexpected problem.",
                error=str(e)
            )

        # The final state is emitted BEFORE finalize(): finalize() queues the
        # spoken notice, and the panel needs to know the task has finished.
        _emit_barramento(
            "task",
            id=te.id,
            state={
                "concluida": "completed",
                "cancelada": "cancelled",
                "limite": "limit",
            }.get(result.state, result.state),
        )

        te.finalize(result)

        try:
            task_memory.register(
                te.id,
                te.request,
                result.state,
                result.response,
                getattr(result,"arquivos",None) or [],
                steps=len(result.steps),
            )
        except Exception as e:
            print(f"[!] Não consegui registrar a task: {e}")

    # ------------------------------------------------------------------

    def _enqueue_confirmation(self,te,request):

        if self.control is None:
            RunningTask.respond(request,WITHOUT_RESPONSE)
            return

        def execute(mic_stream):

            confirmador = ConfirmadorVoice(
                self.control,
                mic_stream,
                prefix="About the background task: "
            )

            decision = confirmador.confirm(
                request.tool,
                request.description,
                request.args
            )

            RunningTask.respond(request,decision)

        self.control.announce(
            text="(confirmation request)",
            origin=f"task {te.id}",
            type="confirmation",
            prioridade=PRIORIDADE_CONFIRMATION,
            task_id=te.id,
            execute=execute,
            valid=lambda: (
                not request.event.is_set()
                and not request.expired
                and not te.cancel_ev.is_set()
            )
        )

    def _announce_result(self,te):
        """Chamada pela thread da task quando ela termina desattached."""

        r = te.result


        # The user cancelled or declined, so there is no need to notify them again.
        if r.state == "cancelada" and (
            te.cancelled_by_user or te.last_decision == NOT
        ):
            return

        if self.control is None:
            print(f"\n[ℹ] Tarefa {te.id} terminou: {r.response}")
            return

        if r.state == "concluida":
            intro = personality.phrase("concluida")
        else:
            intro = personality.phrase("failure")

        response = r.response

        if r.search:
            response = ensure_sources(response,r.search)

        self.control.announce(
            text=f"{intro} {response}",
            origin=f"task {te.id}",
            type="result",
            prioridade=PRIORIDADE_RESULT,
            task_id=te.id,
            title="Background task",
            ao_deliver=lambda message: self._register_history(
                te,message
            )
        )

    @staticmethod
    def _register_history(te,message):
        """Roda na thread principal (a única que mexe no llm.historico)."""

        log = (te.result.log if te.result else "") or "none"

        llm.history.append({
            "role": "system",
            "content": (
                "A background task you started earlier has finished. "
                f"Request: {te.request}. "
                f"Work log (never read it aloud): {log}."
            )
        })

        llm.add_history("assistant",message.text)

        llm._trim_history()

    # ------------------------------------------------------------------

    def respond_about_tasks(self,text):
        """
        Perguntas e pedidos sobre as tarefas em segundo plano ("how is the
        task going?", "cancel the background task"). None se o texto não
        é sobre isso (o loop segue o caminho normal).
        """

        text = text or ""
        active = self.active()

        if _DEFAULT_CANCEL.search(text) and (
            active
            or re.search(r"\bbackground\b",text,re.IGNORECASE)
        ):

            if not active:
                return f"There's no background task running{address_suffix()}."

            for te in active:
                te.cancel()

            return (
                personality.phrase("cancelada")
                + " "
                + ("I'm stopping the background task."
                   if len(active) == 1 else
                   "I'm stopping the background tasks.")
            )

        if _DEFAULT_STATUS.search(text) and (
            active or _MENCIONA_TASK.search(text)
        ):

            if not active:

                if self.control is not None and self.control.pending():
                    return (
                        f"Nothing is running{address_suffix()}, but I have "
                        "something to tell you. I'll say it in a moment."
                    )

                return personality.phrase("sem_tarefa")

            te = active[-1]
            p = te.progress()

            if len(active) > 1:
                parts = f"I'm working on {len(active)} background tasks. "
            else:
                parts = "I'm working on a background task. "

            if p["decorrido_min"] >= 1:
                parts += f"It's been {p['decorrido_min']} minute" + (
                    "s" if p["decorrido_min"] != 1 else ""
                ) + ". "

            if p["steps"]:
                parts += f"I'm on step {p['steps']}"
                if p["atual"]:
                    parts += f": {p['atual']}"
                parts += "."

            if p["ultimo_ok"] and p["ultimo_ok"] != p["atual"]:
                parts += f" The last step that worked was: {p['ultimo_ok']}."

            return parts.strip()

        return None

    def finish(self):

        with self._lock:
            tasks = list(self._tasks)

        for te in tasks:
            te.cancel(by_user=False)


# ----------------------------------------------------------------------
# single application instance
# ----------------------------------------------------------------------

manager = None


def start_manager(control=None):
    """Chamado no boot (jarvisb.py) com o ControleDeVoz."""

    global manager

    manager = ManagerOfTasks(control)

    return manager


def get_manager():
    """O gerente do boot, ou um sem controle (nada de segundo plano)."""

    global manager

    if manager is None:
        manager = ManagerOfTasks(None)

    return manager