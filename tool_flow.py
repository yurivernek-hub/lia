import time

import llm
from utils import normalize_text
from memory import address_suffix
from events import emit,bus

import background_tasks
from confirmation import VoiceConfirmer  # noqa: F401  (compatibility)

# The search source lives in background_tasks (both paths use the same one).
_ensure_sources = background_tasks.ensure_sources

# Typing one of these in the interface (during the task) interrupts the work.
WORDS_STOP = {
    "stop","cancel","abort","never mind","nevermind","stop it",
    "cancel that","stop that","halt"
}


def _user_requested_stop():
    """Texto digitado 'stop'/'cancel' durante a tarefa: consome e para."""

    text = bus.peek_text()

    if not text:
        return False

    if normalize_text(text).strip(" .!?") in WORDS_STOP:

        bus.get_text()

        return True

    # Any other text typed is queued for the next turn.
    return False


def _speak(voice,text):
    """Fala uma frase da Lia mostrando-a na interface (como as outras)."""

    print(f"\n{text}\n")

    emit("state",state="speaking")
    emit("response_end",text=text)

    voice.speak(text)
    voice.wait_end()


def process_with_tools(command,voice,mic_stream,second_plan=False):
    """
    Tarefa em várias etapas (tarefa.py), numa thread (tarefas_bg.py).

    Em primeiro plano a Lia espera, atendendo confirmações (sudo, por voz
    ou botão) e "stop" digitado, e fala o resultado. Se a tarefa passar de
    TAREFA_PRIMEIRO_PLANO_SEGUNDOS ela vai pro segundo plano: a Lia avisa,
    volta a ouvir, e o resultado entra na fila do ControleDeVoz.
    Com segundo_plano=True (pedido explícito) nasce já em segundo plano.
    Pedido simples (clima, abrir app, pesquisa) é uma tarefa de um passo.
    """

    ger = background_tasks.get_manager()
    context = list(llm.history)

    te = ger.start(command,context,second_plan=second_plan)

    if te is None:

        n = ger.max_simultaneas
        how_many = {1: "one",2: "two",3: "three"}.get(n,str(n))

        _speak(
            voice,
            f"I'm already working on {how_many} background "
            f"task{'s' if n > 1 else ''}{address_suffix()}. Let me finish one "
            "first, or tell me to cancel the background task."
        )

        return

    if second_plan:

        response = (
            f"Understood{address_suffix()}. I'll work on that in the "
            "background and let you know when it's done."
        )

        _speak(voice,response)

        llm.add_history("user",command)
        llm.add_history("assistant",response)

        return

    # ---- foreground: wait for the task, handling any requests it makes ----

    confirmador = VoiceConfirmer(voice,mic_stream)
    start = time.time()
    avisou = False

    def pending_warning():
        return (
            not avisou and te.n_steps >= 1 and not te.first_confirm
        )

    while True:

        # One notice only, on the first step (the sudo prompt already asks aloud).
        if pending_warning():

            voice.speak(f"One moment{address_suffix()}.")
            avisou = True

        if te.finished.wait(0.05):
            break

        request = te.request_pending()

        if request:

            te.respond(
                request,
                confirmador.confirm(
                    request.tool,request.description,request.args
                )
            )

            continue

        if _user_requested_stop():
            te.cancel()

        if (
            ger.first_plan
            and time.time() - start >= ger.first_plan
            and ger.detach(te)
        ):

            response = (
                f"This one is taking a while{address_suffix()}. I'll keep "
                "working on it in the background and tell you when it's done."
            )

            _speak(voice,response)

            llm.add_history("user",command)
            llm.add_history("assistant",response)

            return

    result = te.result

    if pending_warning():
        voice.speak(f"One moment{address_suffix()}.")

    if result.state == "error":

        emit("state",state="error",message=result.error)

        voice.speak(result.response)
        voice.wait_end()

        return

    response = result.response

    if result.search:
        response = _ensure_sources(response,result.search)

    _speak(voice,response)

    # Conversation history: only the request, a summary of what was done, and the
    # response (steps stay in the Task, not in global history).
    llm.add_history("user",command)

    if result.log:

        llm.history.append({
            "role": "system",
            "content": (
                "Task log (for your reference only, never read it aloud): "
                + result.log
            )
        })

    llm.add_history("assistant",response)