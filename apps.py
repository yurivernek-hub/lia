import os
import re
import glob
import shlex
import shutil
import subprocess

from utils import normalize_text


def open_command(command):
    """
    Dispara um processo desacoplado (não bloqueia o Jarvis, não
    herda o terminal dele). Recebe uma lista de argv — nunca uma
    string com shell=True — então não há risco de injeção de shell.
    """

    try:

        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

        return True

    except Exception:

        return False


# Standard Linux .desktop shortcut directories, covering native,
# Flatpak, and Snap installations.
DIRECTORIES_DESKTOP_ENTRIES = [
    "/usr/share/applications",
    "/usr/local/share/applications",
    os.path.expanduser("~/.local/share/applications"),
    "/var/lib/snapd/desktop/applications",
    "/var/lib/flatpak/exports/share/applications",
    os.path.expanduser(
        "~/.local/share/flatpak/exports/share/applications"
    ),
]

# Common aliases that differ from the actual executable name.
ALIASES_APPLICATIONS = {
    "vs code": "code",
    "vscode": "code",
    "visual studio code": "code",
    "chrome": "google-chrome",
    "google chrome": "google-chrome",
    "navegador": "firefox",
    "gerenciador de arquivos": "nautilus",
    "arquivos": "nautilus",
    "calculadora": "gnome-calculator",
    "file manager": "dolphin",
    "files": "dolphin",
    "web browser": "firefox",
    "browser": "firefox",
    "terminal": "konsole",
    "calculator": "kcalc",
}


def _list_desktop_entries():

    entradas = []

    for directory in DIRECTORIES_DESKTOP_ENTRIES:

        entradas.extend(
            glob.glob(os.path.join(directory,"*.desktop"))
        )

    return entradas


def _read_field_desktop(path,field):
    """
    Lê um campo (ex: "Name", "Exec") da seção [Desktop Entry] de um
    arquivo .desktop, ignorando localizações (ex: "Name[pt_BR]").
    """

    try:

        with open(path,"r",encoding="utf-8",errors="ignore") as f:

            dentro_secao_main = False

            for line in f:

                line = line.strip()

                if line.startswith("["):
                    dentro_secao_main = (
                        line == "[Desktop Entry]"
                    )
                    continue

                if (
                    dentro_secao_main
                    and line.startswith(f"{field}=")
                ):
                    return line.split("=",1)[1].strip()

    except Exception:
        pass

    return None


def _resolve_command_desktop(exec_raw):
    """
    Converte o valor de "Exec=" de um .desktop num argv utilizável,
    removendo os field codes (%u, %U, %f, %F, %c, %k, %i).
    """

    if not exec_raw:
        return None

    try:
        parts = shlex.split(exec_raw)
    except ValueError:
        parts = exec_raw.split()

    parts = [
        p for p in parts
        if not re.fullmatch(r"%[a-zA-Z]",p)
    ]

    return parts or None


def tool_open_application(name=""):
    """
    Abre um aplicativo instalado pelo nome, em duas etapas:
    1) tenta o binário direto no PATH;
    2) se não achar, procura um atalho .desktop cujo "Name" bata
       com o nome pedido (Flatpak/Snap etc).
    """

    if not name or not name.strip():

        return {
            "success": False,
            "error": "No application name was given."
        }

    name_normalized = normalize_text(name)

    candidatos_binario = [
        ALIASES_APPLICATIONS.get(name_normalized,name_normalized),
        name_normalized.replace(" ",""),
        name_normalized.replace(" ","-"),
    ]

    for candidato in candidatos_binario:

        path_bin = shutil.which(candidato)

        if not path_bin:
            continue

        if open_command([path_bin]):
            return {
                "success": True,
                "metodo": "binario_no_path",
                "command": path_bin
            }

        return {
            "success": False,
            "error": f"I found the binary \"{path_bin}\" but "
                    f"failed to start it."
        }

    shortcut_correspondente = None

    for path_desktop in _list_desktop_entries():

        name_app = _read_field_desktop(path_desktop,"Name")

        if not name_app:
            continue

        name_app_normalized = normalize_text(name_app)

        if name_app_normalized == name_normalized:
            shortcut_correspondente = path_desktop
            break

        if (
            not shortcut_correspondente
            and name_normalized in name_app_normalized
        ):
            shortcut_correspondente = path_desktop

    if shortcut_correspondente:

        command = _resolve_command_desktop(
            _read_field_desktop(shortcut_correspondente,"Exec")
        )

        if command and open_command(command):

            return {
                "success": True,
                "metodo": "atalho_desktop",
                "command": " ".join(command)
            }

    return {
        "success": False,
        "error": f"I couldn't find \"{name}\" installed, neither as a "
                f"binary in the PATH nor as an application shortcut."
    }