"""
Execução de tarefas em várias etapas (o "agent" da Lia).

Uma Tarefa conversa com o modelo em ciclo: o modelo pede uma ferramenta,
a Tarefa executa de verdade, devolve o resultado REAL e o modelo decide o
próximo passo (outra ferramenta, corrigir um erro, testar de novo) até
terminar ou até a Tarefa explicar por que não conseguiu.

Por que existe separada do fluxo de voz:
  - NÃO sabe de voz, microphone nem barramento: tudo entra por parâmetros
    (confirmador, cancelado, ao_passo, emitir, tools). Hoje o fluxo
    de voz roda uma Tarefa na thread principal; o MVP 2 roda a MESMA
    Tarefa numa thread de segundo plano, só trocando os parâmetros.
  - Tem a lista de mensagens PRÓPRIA (o llm.historico global é cortado em
    MAX_HISTORICO e cada passo gasta duas mensagens: uma task longa
    perderia o próprio pedido). Só o resumo final vai pro histórico global.

Regras:
  - Confirmação só pra comando com sudo (tools.exige_confirmation).
  - Limites: passos (iterações do modelo), tempo total e falhas repetidas
    da MESMA call. Estourou: o modelo é chamado SEM tools pra
    explicar o que fez e por que parou.
  - Nada de "deu certo" sem evidência: se a última execução de comando
    failed e o modelo tenta encerrar, ele é avisado uma vez.
"""

import json
import math
import re
import time
from dataclasses import dataclass, field

import groq

from memory import address_suffix
import config
import llm
from events import emit as _emit_barramento
from tools import (
    TOOLS_GROQ,
    FUNCTIONS_DISPONIVEIS,
    set_cancellation,
    describe_call_tool,
    requires_confirmation,
    generate_response_tool,
)
from utils import clear_text_for_voice

# Tunable settings from config.py; these values are used if a setting is absent.
MAX_STEPS = getattr(config,"TAREFA_MAX_PASSOS",12)
LIMIT_SECONDS = getattr(config,"TAREFA_LIMITE_SEGUNDOS",240)
MAX_TOKENS = getattr(config,"TAREFA_MAX_TOKENS",4096)
MAX_RESULT = getattr(config,"TAREFA_MAX_RESULTADO",3000)
MAX_REPETICOES = 3
MANTER_RESULTS = 6      # tool results retained in full
CORTE_RESULT_OLD = 300

# Confirmer responses.
YES = "sim"
NOT = "nao"
WITHOUT_RESPONSE = "sem_resposta"


MODO_TASK = """

=== TASK MODE ===
You can use tools several times in a row to finish a task. After each
tool result, decide what to do next: call another tool, or stop and give
the final answer.
- For code tasks: write the file, RUN it, read the real output, and fix
  errors by editing and running again, until it works. After a fix, run
  it again to confirm.
- Use editar_arquivo for small fixes to an existing file; use
  criar_arquivo for new files or full rewrites. Keep files short enough
  to be written in one go.
- Commands run without a keyboard: never write a script that waits for
  input.
- Commands have NO time limit: a long-running command is fine and runs
  until it finishes or the user cancels the task. Do not shorten or split
  a command just to make it faster.
- Environment: Linux with KDE, python3. For CPU temperature read
  /sys/class/hwmon/hwmon*/name and temp*_input (millidegrees Celsius):
  the CPU chips are k10temp (AMD), coretemp (Intel) or zenpower;
  spd5118 is RAM, nvme is the SSD, amdgpu or nouveau the GPU. A log must
  hold real values (numbers with units), never a chip name or a label.
- Running without errors is not enough: check that the result is RIGHT.
  For a computation, print the intermediate data too (for example, the
  list of primes that were summed) and sanity-check it, or cross-check
  with a second method, before reporting.
- Never say something worked unless a tool result showed it. Report
  numbers exactly as the tool printed them; never compute or guess a
  number yourself.
- If a step fails, read the error and change your approach. Do not
  repeat the exact same call.
- If you cannot finish after genuine attempts, say so and explain why.
- Do only what the user asked. Do not delete or overwrite things they did
  not mention.
- If you notice the result does not meet the request, fix it with tools
  now. Do not report it and ask the user to adjust it.
- If the user asked to see results, say the key values (for example the
  lowest and highest reading), not just that the script ran.
- Your final message is spoken aloud: 2 to 4 short sentences of plain
  spoken English, no markdown. Do not start it with the user's name or
  title. Never read code, long paths or raw output
  aloud: summarize the real result (for example, the value that was
  printed).
"""


class Confirmer:
    """
    Quem pergunta ao usuário "posso?". confirmar() devolve SIM, NAO ou
    SEM_RESPOSTA. Em primeiro plano é por voz/botão (tool_flow);
    em segundo plano (MVP 2) o pedido entra na fila de mensagens.
    """

    def confirm(self,tool,description,args):
        raise NotImplementedError


@dataclass
class ResultTask:
    state: str                 # completed | cancelled | limit | error
    response: str               # what Lia should say
    steps: list = field(default_factory=list)
    log: str = ""               # short summary of what was done (for the history)
    search: dict = None       # latest successful web search (sources)
    error: str = ""              # short message for the interface (error state)
    files: list = field(default_factory=list)   # changed files (task memory)


class Task:

    def __init__(
        self,
        request,
        context=None,
        confirmador=None,
        cancelled=None,
        ao_step=None,
        emit=None,
        tools=None,
        task_id=None,
        max_steps=None,
        limit_seconds=None,
        bloquear=None,
    ):

        self.request = request
        self.context = list(context or [])
        self.confirmador = confirmador
        self.cancelled = cancelled or (lambda: False)
        self.ao_step = ao_step
        self.emit = emit if emit is not None else _emit_barramento
        self.id = task_id

        # block(name) -> True: the tool cannot run now (for example,
        # tools that affect the screen when the task runs in the background).
        self.bloquear = bloquear
        self.max_steps = max_steps or MAX_STEPS

        # None = the default (LIMIT_SECONDS); 0 = no time limit.
        self.limit = (
            LIMIT_SECONDS if limit_seconds is None else limit_seconds
        )

        # None = all tools; a list restricts them (the background
        # task should not affect the screen, for example).
        self.allowed = set(tools) if tools else None

        self.tools_groq = [
            f for f in TOOLS_GROQ
            if self.allowed is None
            or f["function"]["name"] in self.allowed
        ]

        self.steps = []
        self.n_tools = 0
        self.search = None
        self._falhas = {}
        self._last_execution_failed = False
        self._last_error_execution = ""
        self._last = None
        self._reflexoes = 0
        self._reflexoes_numbers = 0
        self._escreveu = False
        self._executou_ok = False
        self._revisado = False
        self._evidence = _numbers(request)

    # ------------------------------------------------------------------

    def execute(self):

        messages = (
            [{
                "role": "system",
                "content": llm.instruction_for(self.request) + MODO_TASK
            }]
            + self.context
            + [{"role": "user","content": self.request}]
        )

        self._state("thinking")

        try:

            return self._loop(messages)

        except groq.RateLimitError:

            print("\n[!] Limite de uso do Groq atingido.")

            return self._failure(
                f"I've hit Groq's usage limit for now{address_suffix()}.",
                "Groq usage limit reached."
            )

        except Exception as e:

            print(f"\n[!] Erro no Groq (task): {e}")

            return self._failure(
                "I couldn't reach Groq right now.",
                "Can't reach Groq."
            )

    # ------------------------------------------------------------------

    def _loop(self,messages):

        start = time.time()
        iteracoes = 0

        while True:

            if self.cancelled():
                return self._finish(
                    "cancelada",f"Stopped{address_suffix()}."
                )

            if self.limit and time.time() - start > self.limit:
                return self._force_final(
                    messages,"the time limit for this task was reached"
                )

            if iteracoes >= self.max_steps:
                return self._force_final(
                    messages,"the step limit for this task was reached"
                )

            iteracoes += 1

            self._compact(messages)

            message,reason = self._call_model(messages)

            calls = message.tool_calls or []

            # ---- no tool call: the model wants to finish ----

            if not calls:

                text = clear_text_for_voice(message.content or "")

                if reason == "length" and not text:

                    messages.append({
                        "role": "system",
                        "content": (
                            "Your last output was cut off. Be shorter: "
                            "write smaller files or answer in fewer words."
                        )
                    })

                    continue

                if self._last_execution_failed and self._reflexoes < 1:

                    self._reflexoes += 1

                    messages.append({
                        "role": "system",
                        "content": (
                            "Your last command FAILED"
                            + (
                                f": {self._last_error_execution}"
                                if self._last_error_execution else "."
                            )
                            + " Either keep fixing it with tools, or if "
                              "you truly cannot, say plainly that it "
                              "failed. Do not claim success."
                        )
                    })

                    continue

                without_origin = (
                    self._numbers_without_origin(text)
                    if self.n_tools else []
                )

                if without_origin and self._reflexoes_numbers < 1:

                    self._reflexoes_numbers += 1

                    print(
                        "[↻ Revisão: números sem origem nas tools: "
                        + ", ".join(without_origin) + "]"
                    )

                    messages.append({
                        "role": "system",
                        "content": (
                            "Your answer mentions "
                            + ", ".join(without_origin)
                            + ", but no tool output shows "
                            + ("that number" if len(without_origin) == 1
                               else "those numbers")
                            + ". Use only values the tools really "
                              "printed: correct the answer, or run a "
                              "tool to check first."
                        )
                    })

                    continue

                # Coding task: running without errors does not prove it met the
                # request (for example, a log containing a chip name instead of the
                # temperature). Review it once before finishing.
                if (
                    self._escreveu
                    and self._executou_ok
                    and not self._revisado
                ):

                    self._revisado = True

                    messages.append({
                        "role": "system",
                        "content": (
                            "Before you finish, check your work against "
                            f"the request: \"{self.request[:400]}\". Does "
                            "the REAL tool output show exactly what was "
                            "asked (right quantity, units, items)? If "
                            "something is missing or looks wrong, for "
                            "example a log with no values or a name where "
                            "a number should be, fix it with tools now "
                            "instead of asking the user to adjust. If "
                            "everything is right, give the final answer."
                        )
                    })

                    continue

                return self._concluir(text)

            # ---- with tools: execute them all, in order ----

            analisadas = []

            for c in calls:

                raw = c.function.arguments or "{}"

                try:
                    args = json.loads(raw)
                except ValueError:
                    args = None

                if not isinstance(args,dict):
                    args = None
                    raw = "{}"   # Groq rejects invalid JSON in the history

                analisadas.append((c,args,raw))

            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.function.name or "",
                            "arguments": raw
                        }
                    }
                    for c,_,raw in analisadas
                ]
            })

            stop = None

            for c,args,_ in analisadas:

                if stop:

                    result = {
                        "success": False,
                        "error": "Skipped: the task was stopped."
                    }

                else:

                    result,stop = self._execute_call(c,args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": c.id,
                    "content": self._serializar(result)
                })

            if stop == "cancelada":
                return self._finish(
                    "cancelada",f"Cancelled{address_suffix()}."
                )

            if stop == "sem_resposta":
                return self._finish(
                    "cancelada","I didn't hear a confirmation. Cancelling."
                )

            if stop == "usuario":
                return self._finish(
                    "cancelada",f"Stopped{address_suffix()}."
                )

            if stop == "repetida":
                return self._force_final(
                    messages,"the same step kept failing"
                )

            # The weather tool has already fetched the complete forecast and
            # emitted its widget. Avoid a second model round trip just to
            # restate those same values; let the voice flow speak immediately.
            if (
                len(analisadas) == 1
                and analisadas[0][0].function.name == "mostrar_clima"
                and self._last
                and self._last[0] == "mostrar_clima"
                and self._last[1].get("success")
            ):
                print("[⚡ Forecast widget ready; skipping follow-up model call.]")
                return self._concluir(
                    generate_response_tool("mostrar_clima", self._last[1])
                )

            self._state("thinking")

    # ------------------------------------------------------------------

    def _call_model(self,messages,tools=True):

        parametros = dict(
            model=config.GROQ_MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=MAX_TOKENS,
            extra_body={"reasoning_effort": config.GROQ_REASONING_EFFORT}
        )

        if tools and self.tools_groq:
            parametros["tools"] = self.tools_groq
            parametros["tool_choice"] = "auto"

        tentativas = 0
        start_call = time.perf_counter()

        while True:

            try:

                pacote = llm.GROQ_CLIENT.chat.completions.create(**parametros)

                # Diagnostics: how long each call takes and how many tokens it
                # uses (Groq limits are per minute).
                uso = getattr(pacote,"usage",None)

                print(
                    f"[⏱ Modelo: {time.perf_counter() - start_call:.1f} s"
                    + (
                        f", {getattr(uso,'total_tokens','?')} tokens"
                        if uso else ""
                    )
                    + "]"
                )

                break

            except groq.RateLimitError as e:

                wait = _wait_of_limit(e)

                # A short wait is worthwhile; after a long wait, give up and explain why.
                if tentativas >= 2 or wait > 25:
                    raise

                tentativas += 1

                print(f"\n[ℹ] Limite do Groq: esperando {wait:.0f} s...")

                time.sleep(wait)

        escolhas = getattr(pacote,"choices",None) or []

        if not escolhas:
            raise RuntimeError("The model returned no choices.")

        return escolhas[0].message,getattr(escolhas[0],"finish_reason",None)

    # ------------------------------------------------------------------

    def _execute_call(self,call,args):
        """Devolve (resultado, sinal de parada ou None)."""

        name = call.function.name or ""

        if args is None:

            return {
                "success": False,
                "error": (
                    "The tool arguments were not valid JSON (the output may "
                    "have been cut off). Try again with shorter content."
                )
            },None

        if self.cancelled():

            return {
                "success": False,
                "error": "Skipped: the user stopped the task."
            },"usuario"

        function = FUNCTIONS_DISPONIVEIS.get(name)

        if not function or (
            self.allowed is not None and name not in self.allowed
        ):

            return {
                "success": False,
                "error": f"The tool {name} doesn't exist."
            },None

        if self.bloquear and self.bloquear(name):

            return {
                "success": False,
                "error": (
                    "That tool needs the screen and isn't available while "
                    "working in the background. Use another approach, or "
                    "explain that it can't be done now."
                )
            },None

        description = describe_call_tool(name,args)
        confirm = requires_confirmation(name,args)

        self.n_tools += 1
        n = self.n_tools

        print(f"\n[🔧 Passo {n}: {name}({args})]")

        self.emit(
            "passo",
            n=n,
            tool=name,
            description=description,
            task=self.id
        )

        if self.ao_step:

            try:
                self.ao_step(n,name,args,description,confirm)
            except Exception as e:
                print(f"[!] Erro no aviso de passo: {e}")

        if confirm:

            decision = (
                self.confirmador.confirm(name,description,args)
                if self.confirmador else NOT
            )

            if decision == WITHOUT_RESPONSE:

                return {
                    "success": False,
                    "error": "No confirmation was heard."
                },"sem_resposta"

            if decision != YES:

                return {
                    "success": False,
                    "error": "The user denied this action."
                },"cancelada"

        self._state("executing")

        # A tool (such as a long-running command) needs to know whether the
        # user cancelled the task while it was running.
        set_cancellation(self.cancelled)

        try:
            result = function(**args)
        except TypeError as e:
            result = {
                "success": False,
                "error": f"Invalid arguments for {name}: {e}"
            }
        except Exception as e:
            result = {"success": False,"error": str(e)}
        finally:
            set_cancellation(None)

        if not isinstance(result,dict):
            result = {"success": True,"result": str(result)}

        success = bool(result.get("success"))

        # Also accept rounded numbers: weather may return 28.4 and 30.5,
        # and saying "28" or "31" is normal, not an invented number.
        self._evidence |= _numbers_evidence(
            json.dumps(result,ensure_ascii=False)
        )

        print(f"[🔧 Resultado: {result}]")

        self.emit("result",tool=name,success=success)

        self.steps.append({
            "n": n,
            "ferramenta": name,
            "args": args,
            "success": success
        })

        self._last = (name,result)

        if success and name in ("criar_arquivo","editar_arquivo"):
            self._escreveu = True

        if success and name == "executar_comando":
            self._executou_ok = True

        if name == "executar_comando":

            self._last_execution_failed = not success

            self._last_error_execution = (
                "" if success else
                (
                    result.get("erro_saida")
                    or result.get("error")
                    or ""
                ).strip()[-300:]
            )

        if name == "pesquisar_web" and success:
            self.search = result

        if not success:

            assinatura = (
                name,
                json.dumps(args,sort_keys=True,ensure_ascii=False)
            )

            self._falhas[assinatura] = self._falhas.get(assinatura,0) + 1

            if self._falhas[assinatura] >= MAX_REPETICOES:
                return result,"repetida"

            if self._falhas[assinatura] == MAX_REPETICOES - 1:

                result["notice"] = (
                    "This exact call already failed twice. Change your "
                    "approach instead of repeating it."
                )

        return result,None

    # ------------------------------------------------------------------

    @staticmethod
    def _compact(messages):
        """
        Tarefa longa = muitos resultados. Só os mais recentes ficam por
        inteiro; os antigos viram um resumo curto (economiza tokens e
        evita estourar o limite do Groq). O pedido e as conclusões ficam.
        """

        indices = [
            i for i,m in enumerate(messages) if m["role"] == "tool"
        ]

        for i in indices[:-MANTER_RESULTS]:

            content = messages[i]["content"]

            if len(content) > CORTE_RESULT_OLD + 40:

                messages[i] = {
                    **messages[i],
                    "content": (
                        content[:CORTE_RESULT_OLD]
                        + " ...[older output trimmed]"
                    )
                }

    def _numbers_without_origin(self,text):
        """Números ditos na resposta que nenhuma ferramenta mostrou."""

        return sorted(_numbers(text) - self._evidence)

    def _force_final(self,messages,reason):
        """Estouro de limite: o modelo explica o que fez, sem tools."""

        messages = messages + [{
            "role": "system",
            "content": (
                "Stop using tools now. In 2 to 4 short spoken sentences, "
                "tell the user what you actually managed to do and why you "
                f"could not finish ({reason}). Only claim what a tool "
                "result confirmed."
            )
        }]

        text = ""

        try:

            message,_ = self._call_model(messages,tools=False)

            text = clear_text_for_voice(message.content or "")

        except Exception as e:

            print(f"\n[!] Erro ao pedir o resumo final: {e}")

        if not text:

            text = (
                f"I couldn't finish that{address_suffix()}: {reason}."
            )

        return self._finish("limite",text)

    def _concluir(self,text):

        if not text and self._last:

            text = generate_response_tool(*self._last)

        if not text:
            text = f"Done{address_suffix()}."

        return self._finish("concluida",text)

    def _finish(self,state,response):

        return ResultTask(
            state=state,
            response=response,
            steps=self.steps,
            log=self._log(),
            search=self.search
        )

    def _failure(self,response,message_ui):

        return ResultTask(
            state="error",
            response=response,
            steps=self.steps,
            log=self._log(),
            error=message_ui
        )

    # ------------------------------------------------------------------

    def _state(self,name):

        self.emit("state",state=name)

    @staticmethod
    def _serializar(result):

        return json.dumps(result,ensure_ascii=False)[:MAX_RESULT]

    def _log(self):
        """Resumo curto do que foi feito (vai pro histórico da conversa)."""

        items = []

        for p in self.steps:

            args = p["args"]

            if p["ferramenta"] == "executar_comando":
                target = f" \"{str(args.get('command',''))[:80]}\""
            elif "path" in args:
                target = f" {args['path']}"
            else:
                target = ""

            items.append(
                f"{p['ferramenta']}{target} "
                f"({'ok' if p['success'] else 'failed'})"
            )

        return "; ".join(items)[:600]


_RE_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text):
    """Números (2+ dígitos) de um texto, sem separador de milhar."""

    achados = set()

    for raw in _RE_NUMBER.findall(text or ""):

        n = raw.replace(",","")

        if n.endswith(".0"):
            n = n[:-2]

        if len(n.replace(".","")) >= 2:
            achados.add(n)

    return achados


def _numbers_evidence(text):
    """
    Como _numeros(), mas para o que as FERRAMENTAS mostraram: além de cada
    número, vale ele arredondado (28.4 -> 28 ou 29; 30.5 -> 30 ou 31).
    Sem isso, falar "28 graus" depois de a ferramenta dizer 28.4 parecia
    número inventado e forçava uma call extra ao modelo (lenta).
    """

    achados = _numbers(text)

    for raw in _RE_NUMBER.findall(text or ""):

        try:
            value = float(raw.replace(",",""))
        except ValueError:
            continue

        for inteiro in (math.floor(value),math.ceil(value),int(value + 0.5)):

            if len(str(inteiro)) >= 2:
                achados.add(str(inteiro))

    return achados


def _wait_of_limit(error):
    """Segundos pedidos pelo Groq (cabeçalho retry-after); padrão 5."""

    try:
        return float(error.response.headers.get("retry-after"))
    except Exception:
        return 5.0
