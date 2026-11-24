"""
Integração com o KDE Plasma (Wayland nativo, sem XWayland).

No Wayland uma janela comum não escolhe a própria posição nem fica
"sempre por cima": quem decide é o compositor (KWin). Aqui:

1) garantir_regra(): cria/atualiza uma regra de janela do KWin
   ("jarvisb") com posicionamento centralizado, sempre por cima, sem
   borda, fora da barra de tarefas e sem roubar o foco.
2) carregar_script(): carrega um pequeno script no KWin que
   posiciona o painel no monitor onde está o cursor (o último que
   você mexeu): centralizado na horizontal e perto do topo. Se o
   script falhar, vale o posicionamento "centrado" da regra.
"""

import os
import re
import shutil
import subprocess

GROUP = "jarvisb"
TITLE_WINDOW = "Jarvis"


def _in_kde():

    return "KDE" in os.environ.get("XDG_CURRENT_DESKTOP","").upper()


_LAST_ERROR = ""


def _run(arguments):

    global _LAST_ERROR

    try:

        output = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=5
        )

        _LAST_ERROR = output.stderr.strip()

        return output.stdout.strip()

    except Exception as e:

        _LAST_ERROR = str(e)

        return ""


VERSAO_RULE = "3"

GROUP_ENVIRONMENT = "jarvisb-ambiente"


def ensure_rule():
    """Cria/atualiza as regras de janela. Devolve True se estão em vigor."""

    if not _in_kde():
        return False

    write = shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")
    read = shutil.which("kreadconfig6") or shutil.which("kreadconfig5")

    if not write or not read:
        return False

    def read_key(group,key):
        return _run([read,"--file","kwinrulesrc","--group",group,"--key",key])

    def write_key(group,key,value):
        _run([write,"--file","kwinrulesrc","--group",group,"--key",key,value])

    regras = [
        r for r in read_key("General","rules").split(",") if r
    ]

    if (
        GROUP in regras
        and GROUP_ENVIRONMENT in regras
        and read_key(GROUP,"versao") == VERSAO_RULE
    ):
        return True

    # Code 2 = "Force", 1 = "Do not affect" in the KWin window rule.
    # PANEL: "always on top" and "no focus" are NOT forced (the
    # panel receives clicks/keyboard input and moves behind when you click another
    # window; the KWin script handles that).
    panel = {
        "versao": VERSAO_RULE,
        "Description": "Jarvis B - painel",
        "title": TITLE_WINDOW,
        "titlematch": "1",
        "placement": "Centered",
        "placementrule": "2",
        "positionrule": "1",
        "aboverule": "1",
        "acceptfocusrule": "1",
        "noborder": "true",
        "noborderrule": "2",
        "skiptaskbar": "true",
        "skiptaskbarrule": "2",
        "skippager": "true",
        "skippagerrule": "2",
        "skipswitcher": "true",
        "skipswitcherrule": "2",
    }

    # ENVIRONMENT: below everything, borderless, excluded from the taskbar, no
    # focus ("Jarvis Ambient ..." = title that STARTS this way -> substring).
    environment = {
        "Description": "Jarvis B - ambiente (desktop)",
        "title": "Jarvis Ambient",
        "titlematch": "2",
        "below": "true",
        "belowrule": "2",
        "noborder": "true",
        "noborderrule": "2",
        "skiptaskbar": "true",
        "skiptaskbarrule": "2",
        "skippager": "true",
        "skippagerrule": "2",
        "skipswitcher": "true",
        "skipswitcherrule": "2",
        "acceptfocus": "false",
        "acceptfocusrule": "2",
    }

    for group,configuration in ((GROUP,panel),(GROUP_ENVIRONMENT,environment)):

        for key,value in configuration.items():
            write_key(group,key,value)

    # Keys from older versions of the panel rule.
    for obsoleta in ("position","above","acceptfocus"):
        _run([
            write,"--file","kwinrulesrc","--group",GROUP,
            "--key",obsoleta,"--delete"
        ])

    changed = False

    for group in (GROUP,GROUP_ENVIRONMENT):

        if group not in regras:
            regras.append(group)
            changed = True

    if changed:

        write_key("General","rules",",".join(regras))
        write_key("General","count",str(len(regras)))

    _run([
        "dbus-send","--session","--type=method_call",
        "--dest=org.kde.KWin","/KWin","org.kde.KWin.reconfigure"
    ])

    return True


def load_script(path_js):
    """Carrega (ou recarrega) o script de posição. Devolve (ok, detalhe)."""

    if not _in_kde():
        return False,"não é KDE"

    if not os.path.isfile(path_js):
        return False,f"arquivo não encontrado: {path_js}"

    dbus = shutil.which("dbus-send")

    if not dbus:
        return False,"dbus-send não encontrado"

    base = [dbus,"--session","--print-reply","--dest=org.kde.KWin"]

    _run(base + [
        "/Scripting","org.kde.kwin.Scripting.unloadScript",
        f"string:{GROUP}"
    ])

    output = _run(base + [
        "/Scripting","org.kde.kwin.Scripting.loadScript",
        f"string:{path_js}",f"string:{GROUP}"
    ])

    achado = re.search(r"int32\s+(-?\d+)",output)

    if not achado or int(achado.group(1)) < 0:
        return False,f"loadScript falhou: {output or _LAST_ERROR}"

    number = achado.group(1)

    for objeto in (f"/Scripting/Script{number}",f"/{number}"):

        retorno = _run(base + [objeto,"org.kde.kwin.Script.run"])

        if "method return" in retorno:
            return True,""

    return False,f"run falhou: {_LAST_ERROR}"


def log_script(how_many=8):
    """Últimas mensagens do script (print() do KWin vai pro journal)."""

    journalctl = shutil.which("journalctl")

    if not journalctl:
        return []

    for escopo in (["--user"],[]):

        output = _run(
            [journalctl] + escopo + [
                "-b","-r","-n",str(how_many),"-g","jarvisb:",
                "--no-pager","-o","cat"
            ]
        )

        if output:
            return list(reversed(output.splitlines()))

    return []


if __name__ == "__main__":

    # Quick diagnostics:  python3 kde.py
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "kwin_center.js"
    )

    print("KDE:",_in_kde(),"| regra:",ensure_rule())
    print("script:",load_script(path))
    print("log:",*log_script(),sep="\n  ")