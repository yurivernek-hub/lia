import os
import sys
import getpass

from config import FILE_KEY_GROQ


def _read_key_of_file(path,variable="GROQ_API_KEY"):
    """
    Lê uma variável (padrão GROQ_API_KEY) de um arquivo no formato
    KEY=value (accepts a leading "export ", quotes, and comments starting with #).
    """

    if not os.path.isfile(path):
        return ""

    try:

        modo = os.stat(path).st_mode & 0o777

        if modo & 0o077:

            print(
                f"[!] {path} está legível por outros usuários "
                f"(permissão {oct(modo)}). Recomendo: "
                f"chmod 600 {path}"
            )

        with open(path,"r",encoding="utf-8") as f:

            for line in f:

                line = line.strip()

                if not line or line.startswith("#"):
                    continue

                if line.startswith("export "):
                    line = line[len("export "):].strip()

                name,separador,value = line.partition("=")

                if separador and name.strip() == variable:
                    return value.strip().strip("\"'")

    except Exception as e:
        print(f"[!] Não consegui ler {path}: {e}")

    return ""


def _save_key_in_file(path,key,variable="GROQ_API_KEY"):

    os.makedirs(os.path.dirname(path),mode=0o700,exist_ok=True)

    # Preserve the other lines in the file (for example, search API keys);
    # replace only the GROQ_API_KEY line.
    other_lines = []

    try:

        with open(path,"r",encoding="utf-8") as f:

            for line in f.read().splitlines():

                clear = line.strip()

                if clear.startswith("export "):
                    clear = clear[len("export "):].strip()

                if clear.partition("=")[0].strip() != variable:
                    other_lines.append(line)

    except OSError:
        pass

    other_lines.append(f'{variable}="{key}"')

    # Create it with permission 600 from the outset (there is never a window when the
    # file is readable by other users).
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600
    )

    with os.fdopen(fd,"w",encoding="utf-8") as f:
        f.write("\n".join(other_lines) + "\n")


def load_key_optional(variable):
    """
    Chave que pode não existir (ex: TAVILY_API_KEY): variável de
    ambiente ou arquivo de configuração, sem perguntar nada.
    """

    key = os.environ.get(variable,"").strip()

    if key:
        return key

    return _read_key_of_file(FILE_KEY_GROQ,variable)


def load_key_groq():
    """
    Ordem: variável de ambiente > arquivo de configuração > pergunta
    uma vez no terminal (sem mostrar o que é digitado) e salva no
    arquivo pras próximas execuções.
    """

    key = os.environ.get("GROQ_API_KEY","").strip()

    if key:
        return key

    key = _read_key_of_file(FILE_KEY_GROQ)

    if key:
        return key

    if not sys.stdin.isatty():
        return ""

    print(
        "\nNenhuma chave do Groq encontrada. Cole a sua (grátis em "
        "https://console.groq.com/keys).\n"
        f"Ela será salva em {FILE_KEY_GROQ} (permissão 600) "
        "e você não precisará digitar de novo."
    )

    try:
        key = getpass.getpass("GROQ_API_KEY: ").strip()
    except (EOFError,KeyboardInterrupt):
        return ""

    if key:

        try:
            _save_key_in_file(FILE_KEY_GROQ,key)
            print(f"✓ Chave salva em {FILE_KEY_GROQ}")
        except Exception as e:
            print(f"[!] Não consegui salvar a chave: {e}")

    return key