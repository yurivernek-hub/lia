from memory import address_suffix
import os
import re
import signal
import subprocess
import threading
import time
from calendar_tools import tool_query_calendars

from config import (
    FOLDER_WORK,
    FOLDERS_ALLOWED,
    FOLDERS_NAMED,
)
from apps import tool_open_application
from web_search import tool_search_web
from widgets import tool_show_weather,tool_show_news
from utils import normalize_text
import reminders
import task_memory


def is_safe_path(path):

    if not path:
        return None

    if os.path.isabs(path):

        target = path

    else:

        parts = path.replace("\\","/").split("/",1)
        first_part = parts[0].strip().lower()
        resto = parts[1] if len(parts) > 1 else ""

        if first_part in FOLDERS_NAMED:

            base = FOLDERS_NAMED[first_part]
            target = os.path.join(base,resto) if resto else base

        else:

            target = os.path.join(FOLDER_WORK,path)

    target_resolvido = os.path.realpath(target)

    for folder in FOLDERS_ALLOWED:

        folder_resolvida = os.path.realpath(folder)

        if (
            target_resolvido == folder_resolvida
            or target_resolvido.startswith(folder_resolvida + os.sep)
        ):
            return target_resolvido

    return None


def tool_create_file(path="",content=""):

    target = is_safe_path(path)

    if not target:
        return {
            "success": False,
            "error": "Path is outside the allowed folders."
        }

    try:

        os.makedirs(os.path.dirname(target),exist_ok=True)

        with open(target,"w",encoding="utf-8") as f:
            f.write(content)

        return {"success": True,"path": target}

    except Exception as e:
        return {"success": False,"error": str(e)}


def tool_read_file(path=""):

    target = is_safe_path(path)

    if not target or not os.path.isfile(target):
        return {
            "success": False,
            "error": "File not found or outside the allowed "
                    "folders."
        }

    try:

        with open(target,"r",encoding="utf-8",errors="replace") as f:
            content = f.read()

        return {"success": True,"content": content[:4000]}

    except Exception as e:
        return {"success": False,"error": str(e)}


def tool_list_directory(path=""):

    target_raw = path if path else FOLDER_WORK
    target = is_safe_path(target_raw)

    if not target or not os.path.isdir(target):
        return {
            "success": False,
            "error": "Folder not found or outside the allowed ones."
        }

    try:
        items = os.listdir(target)
        return {"success": True,"items": items}

    except Exception as e:
        return {"success": False,"error": str(e)}



LIMIT_OUTPUT = 2000

# Commands have NO time limit: they run until they finish or the user
# cancels the task. (The names remain defined as None so we do not
# break modules that import them.)
TIMEOUT_DEFAULT = None
TIMEOUT_MAXIMUM = None

# The task runner sets this for the current thread so it can detect
# whether the user cancelled. It is thread-local: background tasks run in
# separate threads, each with its own cancellation state.
_context = threading.local()


def set_cancellation(function):
    """funcao() -> True quando a tarefa atual foi cancelada. None limpa."""

    _context.cancelled = function


def _cancelled():

    function = getattr(_context,"cancelado",None)

    try:
        return bool(function and function())
    except Exception:
        return False


def _cut(text,limit=LIMIT_OUTPUT):
    """Corta no MEIO: o começo e o fim (onde costuma estar o erro) ficam."""

    text = text or ""

    if len(text) <= limit:
        return text

    start = limit // 3

    return (
        text[:start]
        + "\n...[output truncated]...\n"
        + text[-(limit - start):]
    )


def tool_edit_file(path="",old_text="",new_text=""):
    """Troca UM trecho de um arquivo existente (sem reescrever tudo)."""

    target = is_safe_path(path)

    if not target or not os.path.isfile(target):
        return {
            "success": False,
            "error": "File not found or outside the allowed folders."
        }

    if not old_text:
        return {
            "success": False,
            "error": "trecho_antigo must not be empty."
        }

    try:

        with open(target,"r",encoding="utf-8") as f:
            content = f.read()

        ocorrencias = content.count(old_text)

        if ocorrencias == 0:
            return {
                "success": False,
                "error": (
                    "The text to replace was not found. Read the file "
                    "again to see its exact current content."
                )
            }

        if ocorrencias > 1:
            return {
                "success": False,
                "error": (
                    f"The text to replace appears {ocorrencias} times. "
                    "Include more surrounding lines to make it unique."
                )
            }

        with open(target,"w",encoding="utf-8") as f:
            f.write(content.replace(old_text,new_text,1))

        return {"success": True,"path": target}

    except Exception as e:
        return {"success": False,"error": str(e)}


def requires_confirmation(name,args):
    """Regra única de confirmação: só comando com sudo (como sempre foi)."""

    return name == "executar_comando" and bool(
        re.search(
            r"\bsudo\b",
            str((args or {}).get("command","")),
            re.IGNORECASE
        )
    )


def _kill_group(process):
    """Encerra o shell e tudo o que ele iniciou (grupo próprio)."""

    try:
        os.killpg(process.pid,signal.SIGTERM)
    except OSError:
        process.terminate()

    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:

        try:
            os.killpg(process.pid,signal.SIGKILL)
        except OSError:
            process.kill()


def _run_shell(command,timeout=None):
    """
    Roda sem sudo num grupo de processs próprio e sem stdin: um input()
    num script falha na hora em vez de ler o terminal da Lia.

    Sem limite de tempo (timeout None): roda até acabar. A cada 0,5 s
    confere se o usuário cancelou a tarefa; se sim, mata o grupo INTEIRO.

    Devolve (codigo, saida, erro, motivo), com motivo None (terminou
    sozinho), "cancelado" ou "tempo" (só se um timeout foi pedido).
    """

    process = subprocess.Popen(
        command,
        shell=True,
        cwd=FOLDER_WORK,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True
    )

    start = time.monotonic()

    while True:

        try:

            output,error = process.communicate(timeout=0.5)

            return process.returncode,output,error,None

        except subprocess.TimeoutExpired:
            pass

        reason = None

        if _cancelled():
            reason = "cancelado"
        elif timeout is not None and time.monotonic() - start >= timeout:
            reason = "tempo"

        if reason:

            _kill_group(process)

            try:
                output,error = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                output,error = "",""

            return process.returncode,output,error,reason


def tool_execute_command(command="",timeout=None):

    usa_executor = bool(
        re.search(r"\bsudo\b",command,re.IGNORECASE)
    )

    # No limit by default. A timeout applies only when explicitly requested
    # (positive); 0, empty, or invalid means no limit.
    try:
        limit = int(timeout) if timeout else None
    except (TypeError,ValueError):
        limit = None

    if limit is not None and limit < 1:
        limit = None

    try:

        if usa_executor:

            result = subprocess.run(
                [
                    "sudo",
                    "-n",
                    "/usr/local/bin/jarvisb-exec",
                    command
                ],
                cwd=FOLDER_WORK,
                capture_output=True,
                text=True,
                timeout=300
            )

            codigo = result.returncode
            output = result.stdout
            error = result.stderr

        else:

            codigo,output,error,reason = _run_shell(command,limit)

            if reason == "cancelado":
                return {
                    "success": False,
                    "error": (
                        "The command was stopped because the user "
                        "cancelled the task."
                    ),
                    "saida": _cut(output),
                    "erro_saida": _cut(error)
                }

            if reason == "tempo":
                return {
                    "success": False,
                    "error": (
                        f"The command exceeded its time limit ({limit} s) "
                        "and was stopped."
                    ),
                    "saida": _cut(output),
                    "erro_saida": _cut(error)
                }

        return {
            "success": codigo == 0,
            "saida": _cut(output),
            "erro_saida": _cut(error),
            "codigo_retorno": codigo
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "The command exceeded its time limit."
        }

    except Exception as e:
        return {"success": False,"error": str(e)}



FUNCTIONS_DISPONIVEIS = {
    "criar_lembrete": reminders.tool_create_reminder,
    "listar_lembretes": reminders.tool_list_reminders,
    "cancelar_lembrete": reminders.tool_cancel_reminder,
    "consultar_tarefas_recentes": task_memory.tool_query_tasks_recent,
    "criar_arquivo": tool_create_file,
    "ler_arquivo": tool_read_file,
    "listar_diretorio": tool_list_directory,
    "executar_comando": tool_execute_command,
    "editar_arquivo": tool_edit_file,
    "abrir_aplicativo": tool_open_application,
    "pesquisar_web": tool_search_web,
    "mostrar_clima": tool_show_weather,
    "mostrar_noticias": tool_show_news,
    "consultar_calendarios": tool_query_calendars,
}

# Read-only tools have no effect on the computer and run directly,
# without asking for voice confirmation.
TOOLS_WITHOUT_CONFIRMATION = {
    "criar_lembrete",
    "listar_lembretes",
    "cancelar_lembrete",
    "consultar_tarefas_recentes",
    "pesquisar_web",
    "mostrar_clima",
    "mostrar_noticias",
    "consultar_calendarios",
}

TOOLS_GROQ_DEFINICOES = [
    {
        "name": "criar_lembrete",
        "description": (
            "Sets a reminder or timer that Lia will announce aloud at the "
            "right time, even while she is working on something else. Use "
            "it for 'remind me in 30 minutes', 'at 15:00 tell me to call', "
            "or 'warn me 15 minutes before'. Give EITHER minutes from now "
            "OR a clock time HH:MM (24 hour, local). Use antecedencia_minutos "
            "for an advance warning before the time (for a meeting or a "
            "deadline). Do not use it for things that should happen now."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "What to remind, in a few words, phrased as the "
                        "thing to do or the event (e.g. 'stretch', "
                        "'the call with the client')."
                    )
                },
                "minutes": {
                    "type": "number",
                    "description": "Minutes from now. Use 0 if giving a clock time."
                },
                "schedule_time": {
                    "type": "string",
                    "description": "Clock time as HH:MM, 24 hour. Empty if using minutes."
                },
                "advance_notice_minutes": {
                    "type": "integer",
                    "description": "Advance warning in minutes before the time. 0 for none."
                }
            },
            "required": ["message"]
        }
    },
    {
        "name": "listar_lembretes",
        "description": "Lists the reminders and timers that are still pending.",
        "parameters": {"type": "object","properties": {}}
    },
    {
        "name": "cancelar_lembrete",
        "description": "Cancels a pending reminder by its number (see listar_lembretes).",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer","description": "The reminder number."}
            },
            "required": ["id"]
        }
    },
    {
        "name": "consultar_tarefas_recentes",
        "description": (
            "Searches the recent tasks Lia has done: what was asked, how it "
            "ended, and which files were created. Use it when the user refers "
            "to earlier work ('the script we made this morning', 'run that "
            "again', 'where is the file from before'). Pass a few keywords "
            "from the user's words, or leave term empty for the latest tasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "term": {
                    "type": "string",
                    "description": "Keywords to search for. Empty for the most recent tasks."
                }
            }
        }
    },
    {
        "name": "criar_arquivo",
        "description": (
            "Creates a new text or code file inside an allowed folder "
            "(workspace, Downloads, Desktop, Documents, Music, Pictures, "
            "Videos or Trash) with the given content. Overwrites if it "
            "already exists."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "File name or relative path "
                        "(e.g. 'script.py', 'notes/idea.txt')."
                    )
                },
                "content": {
                    "type": "string",
                    "description": "Full content of the file."
                }
            },
            "required": ["path","content"]
        }
    },
    {
        "name": "ler_arquivo",
        "description": (
            "Reads the content of an existing text file inside one of "
            "the allowed folders."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path of the file to read."
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "listar_diretorio",
        "description": (
            "Lists the files inside an allowed folder."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Folder to list. If empty, lists the default "
                        "working folder."
                    )
                }
            },
            "required": []
        }
    },
    {
        "name": "executar_comando",
        "description": (
            "Runs a terminal command inside the working folder and returns "
            "its real output (stdout, stderr and exit code). Commands run "
            "without a keyboard, so they must not wait for input. There "
            "is NO time limit: a command runs until it finishes or the "
            "user cancels the task, so long-running commands are fine. "
            "Commands requiring administrator privileges must use sudo; "
            "sudo commands go through the configured Jarvis B privileged "
            "executor, and the system itself asks the user for "
            "confirmation, so do not ask for it yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run."
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "editar_arquivo",
        "description": (
            "Edits an existing file by replacing ONE exact snippet with "
            "new text. Prefer this over criar_arquivo for small fixes. "
            "The old snippet must match the file exactly and appear "
            "only once."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path of the file to edit."
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text currently in the file."
                },
                "new_text": {
                    "type": "string",
                    "description": "Text that replaces it."
                }
            },
            "required": ["path","old_text","new_text"]
        }
    },
    {
        "name": "abrir_aplicativo",
        "description": (
            "Opens a graphical application installed on the computer by "
            "name (e.g. Firefox, Discord, Spotify, VS Code, GIMP). Works "
            "for any installed application, not just a fixed list - it "
            "looks for the binary in the PATH and, if not found, for a "
            "matching application shortcut (.desktop; covers native, "
            "Flatpak and Snap installs). Use this tool whenever the user "
            "asks to open, start or launch a program/application by "
            "name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Name of the application as the user said it "
                        "(e.g. 'Discord', 'Visual Studio Code')."
                    )
                }
            },
            "required": ["name"]
        }
    },
    {
        "name": "pesquisar_web",
        "description": (
            "Searches the internet and returns a summary and the main "
            "results. Use it for current information or information that "
            "may have changed (news, weather, exchange rates, prices, "
            "scores, releases, who holds a position today) or when you "
            "are not sure of a fact. Don't use it for stable general "
            "knowledge."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Short, objective search terms, the way you "
                        "would type them into a search engine."
                    )
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "mostrar_clima",
        "description": (
            "Shows a WEATHER CARD on the user's screen (current conditions "
            "and a 3-day forecast) and returns the data. Use it whenever "
            "the user asks about the weather, temperature, rain or the "
            "forecast, including follow-ups like 'and tomorrow?'. Speak "
            "only a short summary: the card already shows the numbers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": (
                        "City name. Leave empty to use the user's default "
                        "city."
                    )
                }
            },
            "required": []
        }
    },
    {
        "name": "consultar_calendarios",
        "description": (
            "Reads upcoming events from the calendars available through "
            "KDE Akonadi, including configured Google and local calendars. "
            "Use when the user asks about their schedule, appointments, "
            "classes or upcoming events. This is a read-only operation. "
            "The days parameter specifies the number of days to query, "
            "from 1 to 31. Default to 7 when unspecified."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to query, from 1 to 31."
                }
            },
            "required": []
        }
    },
    {
        "name": "mostrar_noticias",
        "description": (
            "Shows a NEWS CARD on the user's screen with the main "
            "headlines (about a topic, or the top stories if no topic is "
            "given) and returns them. Use it when the user asks for the "
            "news or headlines. Speak a short summary of the top one or "
            "two, not every headline."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": (
                        "Topic to search the news for (e.g. 'artificial "
                        "intelligence'). Leave empty for the top stories."
                    )
                }
            },
            "required": []
        }
    }
]

# OpenAI-compatible format required by the Groq API.
TOOLS_GROQ = [
    {"type": "function","function": definicao}
    for definicao in TOOLS_GROQ_DEFINICOES
]


def describe_call_tool(name,args):
    """Describe the action for the spoken confirmation."""

    if name == "criar_arquivo":
        return f"create the file \"{args.get('path','?')}\""

    if name == "ler_arquivo":
        return f"read the file \"{args.get('path','?')}\""

    if name == "listar_diretorio":

        folder = args.get("path")

        if folder:
            return f"list the folder \"{folder}\""

        return "list the working folder"

    if name == "executar_comando":
        return f"run the command: {args.get('command','?')}"

    if name == "editar_arquivo":
        return f"edit the file \"{args.get('path','?')}\""

    if name == "abrir_aplicativo":
        return f"open the application \"{args.get('name','?')}\""

    if name == "pesquisar_web":
        return f"search the internet for \"{args.get('query','?')}\""

    return f"run {name}"


EXTENSOES_POR_CATEGORY = {
    "image": {"jpg","jpeg","png","gif","webp","bmp","svg"},
    "code file": {"py","cpp","c","h","js","ts","java","go","rs","sh",
                  "html","css","json","yaml","yml"},
    "document": {"pdf","doc","docx","txt","odt","md"},
    "audio file": {"mp3","wav","flac","ogg"},
    "video": {"mp4","mkv","avi","mov"},
    "archive": {"zip","tar","gz","rar","7z"},
}

PLURAL_CATEGORY = {
    "image": "images",
    "code file": "code files",
    "document": "documents",
    "audio file": "audio files",
    "video": "videos",
    "archive": "archives",
    "other type": "other types",
    "folder or extensionless item": "folders or extensionless items",
}


def classify_item(name):

    if "." not in name:
        return "folder or extensionless item"

    ext = name.rsplit(".",1)[-1].lower()

    for category,extensoes in EXTENSOES_POR_CATEGORY.items():

        if ext in extensoes:
            return category

    return "other type"


def generate_response_tool(name,result):
    """Resposta falada de reserva (se o modelo não formular uma)."""

    if not result.get("success"):

        return (
            f"I couldn't do it{address_suffix()}: "
            f"{result.get('error','unknown error')}"
        )

    if name == "criar_arquivo":
        return f"File created{address_suffix()}."

    if name == "ler_arquivo":

        content = result.get("content","").strip()

        if not content:
            return "The file is empty."

        lines = content.count("\n") + 1

        return (
            f"I read the file{address_suffix()} - it has about {lines} "
            f"lines. The full content is in the terminal."
        )

    if name == "listar_diretorio":

        items = result.get("items",[])

        if not items:
            return "The folder is empty."

        contagem = {}

        for item in items:

            category = classify_item(item)
            contagem[category] = contagem.get(category,0) + 1

        parts_summary = [
            f"{qtd} " + (
                PLURAL_CATEGORY.get(category,category)
                if qtd > 1 else category
            )
            for category,qtd in contagem.items()
        ]

        summary = ", ".join(parts_summary)

        return (
            f"I found {len(items)} items in the folder{address_suffix()}: "
            f"{summary}. The full list with the names is in the terminal."
        )

    if name == "executar_comando":

        output = result.get("saida","").strip()

        if output:

            lines = output.count("\n") + 1

            return (
                f"Command executed successfully{address_suffix()}. It "
                f"produced {lines} line" + ("s" if lines > 1 else "") +
                " of output, available in the terminal."
            )

        return "Command executed successfully, with no output."

    if name == "abrir_aplicativo":
        return f"Opening it{address_suffix()}."

    if name == "pesquisar_web":
        return (
            f"I searched{address_suffix()}, but couldn't summarize the "
            f"results."
        )

    if name == "mostrar_clima":
        city = result.get("city")
        current = result.get("current") or {}
        forecast = result.get("forecast") or []
        parts = []

        temperature = current.get("temperature_c")
        condition = current.get("condition")
        if city and temperature is not None:
            sentence = f"In {city}, it's {temperature} degrees Celsius"
            if condition:
                sentence += f" with {condition}"
            parts.append(sentence + ".")
        elif temperature is not None:
            parts.append(f"It's {temperature} degrees Celsius.")
        elif condition:
            parts.append(f"The current weather is {condition}.")

        for day in forecast[:2]:
            label = day.get("day")
            low, high = day.get("min_c"), day.get("max_c")
            rain = day.get("chance_of_rain_pct")
            if low is None and high is None:
                continue
            when = f"{label}: " if label else ""
            if low is not None and high is not None:
                detail = f"a low of {low} and a high of {high} degrees"
            else:
                value = high if high is not None else low
                detail = f"{value} degrees"
            if rain is not None:
                detail += f", with a {rain} percent chance of rain"
            parts.append(when + detail + ".")

        return " ".join(parts) or f"The forecast is on your screen{address_suffix()}."

    if name == "mostrar_noticias":
        return f"It's on your screen{address_suffix()}."

    return f"Done{address_suffix()}."


# Phrases that indicate a file or system request (verb ... object).
DEFAULT_INTENCAO_SYSTEM = re.compile(
    r"\b("
    r"create|make|write|save|generate|read|list|run|execute|delete|"
    r"remove|erase|open|close|access|show|display|view|look|find|"
    r"search|check|inspect|edit|modify|change|update|copy|move|rename|"
    r"compile|install|uninstall|download|extract|zip|unzip"
    r")\b.*\b("
    r"files?|scripts?|code|folders?|directory|directories|commands?|"
    r"calendars?|agenda|agendas|events?|schedules?|appointments?|classes|"
    r"programs?|projects?|terminal|downloads|documents|desktop|"
    r"pictures|music|videos"
    r")\b",
    re.IGNORECASE
)

# System questions that need no verb ("how much RAM").
DEFAULT_INFO_SYSTEM = re.compile(
    r"\b(cpu|processor|ram|memory usage|disk space|disk usage|"
    r"computer health|system health|health of (?:my )?(?:computer|system)|"
    r"operating system|operational system|os version|kernel version|"
    r"system specs|hardware specs|uptime)\b",
    re.IGNORECASE
)

# Verbs such as "open/start/run" on their own: an application name
# is too arbitrary to fit in a fixed list of nouns.
DEFAULT_OPEN_APP = re.compile(
    r"\b(open|launch|start|run|execute)\b",
    re.IGNORECASE
)

# Questions that probably need current information. This only decides
# the ROUTE (using tools); the model decides whether to actually search.
#
DEFAULT_INTENCAO_WEB = re.compile(
    r"\b(search\w*|google|look up|internet|web|news|weather|forecast|"
    r"temperature in|who won|score|latest|today|yesterday|currently|"
    r"current|price of|how much (?:is|are|does)|exchange rate|stock|"
    r"released?|release date|happening|rain\w*|umbrella|temperature|"
    r"headlines|sunny|cloudy)\b",
    re.IGNORECASE
)

DEFAULT_INTENCAO_CALENDAR = re.compile(
    r"\b(calendar|calendars|agenda|agendas|event|events|"
    r"schedule|schedules|appointment|appointments)\b",
    re.IGNORECASE
)

def looks_like_system_request(text):
    return bool(
        DEFAULT_INTENCAO_SYSTEM.search(text)
        or DEFAULT_INFO_SYSTEM.search(text)
        or DEFAULT_OPEN_APP.search(text)
        or DEFAULT_INTENCAO_WEB.search(normalize_text(text))
        or DEFAULT_INTENCAO_CALENDAR.search(text)
    )
