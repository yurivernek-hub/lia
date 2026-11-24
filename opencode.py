"""
Roteiro de teste do OpenCode como servidor (base da colaboração da Lia).

Roda no SEU computador, com o OpenCode instalado e conectado a um modelo.
Cada cenário responde a uma dúvida que decide como a integração é feita:

  1  Tarefa simples      -> events em tempo real, diff e resposta final
  2  Abortar no meio     -> o abort para de verdade? dá para continuar depois?
  3  Mensagem no meio    -> uma 2ª instrução enfileira ou interrompe?
  4  Voltar com git      -> se a Lia desfaz por fora (git), o OpenCode percebe?

Todos os events recebidos vão para opencode_events.jsonl (uma linha por
evento, com o tempo em segundos). Mande esse arquivo (ou o resumo que o
roteiro imprime no fim) para a Claude analisar.

Uso:
    python opencode.py ~/opencode_workspace
    python opencode.py ~/opencode_workspace --modelo opencode/NOME
    python opencode.py ~/opencode_workspace --cenario 1

Se o OpenCode reclamar de modelo, passe --modelo provedor/modelo (a lista
dos modelos disponíveis aparece no começo).
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import time

from opencode_client import (
    OpenCodeError,
    OpenCodeServer,
    event_idle,
    event_permission,
    session_of_event,
    event_type,
)

FILE_EVENTS = "opencode_events.jsonl"

summary_final = []
start_geral = time.time()

# When the last prompt was sent: displayed times are counted from that point.
last_envio = [time.time()]


# ----------------------------------------------------------------------
# apoio
# ----------------------------------------------------------------------

def title(text):

    print("\n" + "=" * 64)
    print(text)
    print("=" * 64)


def annotate(line):
    """Guarda uma conclusão para o resumo do fim."""

    summary_final.append(line)
    print(f"  >> {line}")


def register(ev,scenario):

    record = dict(ev)
    record["_cenario"] = scenario
    record["_t"] = round(ev.get("_recebido",time.time()) - start_geral,2)

    with open(FILE_EVENTS,"a",encoding="utf-8") as f:
        f.write(json.dumps(record,ensure_ascii=False) + "\n")


def _text_short(ev):
    """Um pedaço legível do evento, para o terminal."""

    def achar(obj,keys):

        if isinstance(obj,dict):

            for key in keys:

                value = obj.get(key)

                if isinstance(value,str) and value.strip():
                    return value

            for value in obj.values():

                achado = achar(value,keys)

                if achado:
                    return achado

        elif isinstance(obj,list):

            for value in obj:

                achado = achar(value,keys)

                if achado:
                    return achado

        return None

    text = achar(ev,("command","file","filePath","title","tool","text","error"))

    return (text or "").replace("\n"," ")[:90]


def empty(client):

    while True:

        try:
            client.events.get_nowait()
        except queue.Empty:
            return


def wait_idle(client,session,scenario,limit=240,scheduled=None):
    """
    Consome os events até a sessão ficar ociosa. Mostra cada evento e
    pergunta no terminal quando o OpenCode pede permissão.

    agendado: lista de (segundos, função) chamadas uma vez, no tempo certo.
    Devolve (terminou, lista_de_events_da_sessao).
    """

    scheduled = sorted(scheduled or [],key=lambda a: a[0])
    start = last_envio[0]           # times counted from when it was sent
    of_session = []
    vistos = 0
    avisou_error = False

    while time.time() - start < limit:

        while scheduled and time.time() - start >= scheduled[0][0]:

            _,function = scheduled.pop(0)
            function()

        try:
            ev = client.events.get(timeout=0.5)
        except queue.Empty:
            continue

        register(ev,scenario)

        owner = session_of_event(ev)

        if owner and owner != session:
            continue

        tipo = event_type(ev)

        # server heartbeats only clutter the terminal (they remain in the file)
        if "heartbeat" in tipo:
            continue

        of_session.append(ev)

        t = ev.get("_recebido",time.time()) - start
        print(f"  [{t:5.1f}s] {tipo:<28} {_text_short(ev)}")

        if "error" in tipo.lower() and not avisou_error:

            avisou_error = True

            print(
                "\n  >>> O OpenCode devolveu um ERRO. Se for sobre modelo ou "
                "provedor, rode de novo com --modelo provedor/modelo "
                "(a lista de modelos aparece no começo).\n"
            )

        permission = event_permission(ev)

        if permission:

            print(f"\n  >>> O OpenCode pede permissão: {permission['title']!r}")

            response = input("  Permitir? [s = esta vez / a = sempre / n = negar]: ")
            response = response.strip().lower()

            codigo = {"s": "once","a": "always"}.get(response,"reject")

            try:
                client.respond_permission(session,permission["id"],codigo)
            except OpenCodeError as e:
                print(f"  [!] Não consegui responder à permissão: {e}")

            continue

        if event_idle(ev):

            # An "idle" event right at the start is just the initial session state.
            if vistos >= 2:
                return True,of_session

        elif owner == session:
            vistos += 1

    return False,of_session


def show_diff(client,session):

    try:
        mudancas = client.diff(session)
    except OpenCodeError as e:
        print(f"  [!] diff falhou: {e}")
        annotate(f"diff: ERRO {str(e)[:100]}")
        return

    if not mudancas:
        annotate("diff: vazio (nenhuma mudança registrada)")
        return

    print(f"  Arquivos alterados segundo o OpenCode: {len(mudancas)}")

    for item in mudancas if isinstance(mudancas,list) else [mudancas]:

        if isinstance(item,dict):

            name = item.get("file") or item.get("path") or "?"
            mais = item.get("additions","?")
            menos = item.get("deletions","?")

            print(f"    - {name}  (+{mais} -{menos})")

    annotate(f"diff: {len(mudancas) if isinstance(mudancas,list) else 1} arquivo(s)")


def git(folder,*arguments):

    result = subprocess.run(
        ["git","-C",folder,*arguments],
        capture_output=True,text=True
    )

    return result.returncode,(result.stdout + result.stderr).strip()


def send(client,session,text,model):

    print(f"\n  Prompt enviado: {text!r}")

    last_envio[0] = time.time()

    client.send(session,text,model=model)


# ----------------------------------------------------------------------
# scenarios
# ----------------------------------------------------------------------

def scenario_1(client,folder,model):

    title("1. Tarefa simples: events, diff e resposta")

    session = client.create_session("teste 1")
    empty(client)

    t0 = time.time()

    send(
        client,session,
        "Create hello.py that prints a greeting and test_hello.py with a "
        "simple test for it. Then run the tests.",
        model
    )

    finished,events = wait_idle(client,session,"1")

    annotate(
        f"cenario 1: {'terminou' if finished else 'NAO terminou no tempo'} em "
        f"{time.time() - t0:.0f}s, {len(events)} events"
    )

    tipos = sorted({event_type(e) for e in events})
    annotate("tipos de evento vistos: " + ", ".join(tipos))

    show_diff(client,session)

    response = client.last_response(session)
    print(f"\n  Resposta final do OpenCode:\n  {response[:400]}")
    annotate(f"resposta final: {'ok' if response else 'VAZIA'}")


def scenario_2(client,folder,model):

    title("2. Abortar no meio e continuar depois")

    session = client.create_session("teste 2")
    empty(client)

    send(
        client,session,
        "Create contagem.py that prints the numbers 1 to 40, one per second "
        "(use time.sleep(1)), then run it.",
        model
    )

    momento = {}

    def abort():

        momento["abort"] = time.time()
        print("\n  >>> ABORTANDO agora")

        try:
            client.abort(session)
        except OpenCodeError as e:
            print(f"  [!] abort falhou: {e}")

    finished,_ = wait_idle(
        client,session,"2",limit=60,scheduled=[(8,abort)]
    )

    if "abort" in momento:
        annotate(
            "abort: sessao ficou ociosa "
            f"{'SIM' if finished else 'NAO'}, "
            f"{time.time() - momento['abort']:.1f}s depois"
        )
    else:
        annotate(
            "abort: NAO TESTADO (a task terminou antes dos 8 s). "
            "Rode de novo com --cenario 2 e um prompt mais demorado."
        )

    codigo,output = git(folder,"status","--short")
    print(f"\n  Estado do git depois do abort:\n  {output or '(limpo)'}")

    # is the session still usable?
    empty(client)
    send(client,session,"Just reply with the word: ok.",model)

    finished,_ = wait_idle(client,session,"2b",limit=60)

    annotate(f"nova mensagem depois do abort: {'respondeu' if finished else 'NAO respondeu'}")
    annotate(f"resposta: {client.last_response(session)[:60]!r}")


def scenario_3(client,folder,model):

    title("3. Mensagem enquanto o OpenCode está ocupado")

    session = client.create_session("teste 3")
    empty(client)

    send(
        client,session,
        "Create a.txt containing the word alpha, then run the shell command "
        "'sleep 10', then say done.",
        model
    )

    def second():

        print("\n  >>> Mandando a SEGUNDA instrução no meio do trabalho")

        try:
            client.send(
                session,"Also create b.txt containing the word beta.",
                model=model
            )
        except OpenCodeError as e:
            print(f"  [!] falhou ao enviar durante o trabalho: {e}")

    finished,events = wait_idle(
        client,session,"3",limit=120,scheduled=[(4,second)]
    )

    # the session may have become idle between the two: wait a little longer
    if finished:

        print("\n  Esperando 8 s para ver se a segunda instrução ainda roda...")

        extra,mais = wait_idle(client,session,"3b",limit=8)
        events += mais

    exists_a = os.path.exists(os.path.join(folder,"a.txt"))
    exists_b = os.path.exists(os.path.join(folder,"b.txt"))

    annotate(f"a.txt existe: {exists_a} | b.txt existe: {exists_b}")
    annotate(
        "segunda instrucao: "
        + ("foi executada" if exists_b else "NAO foi executada")
    )
    annotate(
        "ordem: veja os tempos no arquivo de events (a instrução entrou "
        "em t=4s; compare com quando começou a trabalhar nela)"
    )


def scenario_4(client,folder,model):

    title("4. Voltar uma alteração por fora (git) — o OpenCode percebe?")

    if not os.path.isdir(os.path.join(folder,".git")):
        print("  A pasta não é um repositório git. Pulando.")
        annotate("cenario 4: pulado (pasta sem git)")
        return

    print(f"  ATENÇÃO: este teste faz 'git add -A', 'git commit' e")
    print(f"  'git reset --hard' na pasta:\n    {folder}")

    if input("  Digite 'sim' para continuar: ").strip().lower() != "sim":
        annotate("cenario 4: cancelado por voce")
        return

    git(folder,"add","-A")
    git(folder,"commit","-m","checkpoint antes do teste","--allow-empty")

    _,checkpoint = git(folder,"rev-parse","HEAD")

    session = client.create_session("teste 4")
    empty(client)

    send(
        client,session,
        "Create versao.txt containing exactly: v2. Then run: cat versao.txt",
        model
    )

    wait_idle(client,session,"4")

    git(folder,"add","-A")
    git(folder,"commit","-m","alteracao do opencode")

    print(f"\n  versao.txt existe antes de voltar: "
          f"{os.path.exists(os.path.join(folder,'versao.txt'))}")

    git(folder,"reset","--hard",checkpoint)

    exists = os.path.exists(os.path.join(folder,"versao.txt"))

    print(f"  Voltei com git. versao.txt existe agora: {exists}")
    annotate(f"git reset funcionou: {'SIM' if not exists else 'NAO'}")

    empty(client)

    send(
        client,session,
        "I undid your last change with git, outside of this chat. Check the "
        "current files in the folder and tell me in one sentence whether "
        "versao.txt exists.",
        model
    )

    wait_idle(client,session,"4b")

    response = client.last_response(session)

    print(f"\n  Resposta do OpenCode:\n  {response[:400]}")
    annotate(f"OpenCode disse sobre o arquivo: {response[:120]!r}")


CENARIOS = {"1": scenario_1,"2": scenario_2,"3": scenario_3,"4": scenario_4}


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def show_modelos(client):

    try:
        data = client.providers()
    except OpenCodeError as e:
        print(f"  (não consegui listar os modelos: {e})")
        return

    providers = data.get("providers") if isinstance(data,dict) else data

    if not isinstance(providers,list):
        print(f"  Provedores (formato inesperado): {str(data)[:200]}")
        return

    print("  Provedores e alguns modelos disponíveis:")

    for p in providers:

        modelos = p.get("models") or {}
        ids = list(modelos.keys())[:4] if isinstance(modelos,dict) else []

        print(f"    - {p.get('id')}: {', '.join(ids)}")


def main():

    ap = argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta",nargs="?",default="~/opencode_workspace")
    ap.add_argument("--modelo",default=None,help="provedor/modelo")
    ap.add_argument("--cenario",choices=list(CENARIOS),default=None)
    args = ap.parse_args()

    folder = os.path.abspath(os.path.expanduser(args.folder))

    title(f"Teste do OpenCode na pasta {folder}")

    server = OpenCodeServer(folder)

    try:

        print("  Subindo o servidor...")

        server.start()

        client = server.client()

        print(f"  Servidor no ar (porta {server.port}): {client.health()}")

        show_modelos(client)

        client.start_events()

        escolhidos = [args.scenario] if args.scenario else list(CENARIOS)

        for number in escolhidos:

            try:
                CENARIOS[number](client,folder,args.model)
            except OpenCodeError as e:
                print(f"\n  [!] Erro no cenário {number}: {e}")
                annotate(f"cenario {number}: ERRO {str(e)[:150]}")
            except KeyboardInterrupt:
                print("\n  Cenário interrompido.")
                break

            if number != escolhidos[-1]:
                input("\n  Enter para o próximo cenário... ")

        client.stop_events()

    except OpenCodeError as e:

        print(f"\n[!] {e}")
        return 1

    finally:
        server.stop()

    title("RESUMO (copie e mande para a Claude, junto com o arquivo de events)")

    for line in summary_final:
        print(" -",line)

    print(f"\n  Eventos gravados em: {os.path.abspath(FILE_EVENTS)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
