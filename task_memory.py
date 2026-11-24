"""
Memória das tarefas recentes: o que foi pedido, como terminou e quais
arquivos foram criados ou alterados. Sobrevive a reinícios.

Por que existe: o histórico da conversa é cortado (MAX_HISTORICO) e
some quando a Lia reinicia. Pedidos como "run the script we made this
morning" precisam de um lugar confiável para achar o arquivo.

Guarda até MAX_REGISTROS tarefas em
~/.local/state/jarvisb/tarefas_recentes.json. Só biblioteca padrão.
"""

import datetime as _dt
import json
import os
import re
import threading

FILE = os.path.expanduser("~/.local/state/jarvisb/tarefas_recentes.json")
MAX_RECORDS = 30
MAX_SUMMARY = 240

_trava = threading.Lock()

STOPWORDS = {
    "the","a","an","of","to","in","and","or","for","with","my","me","that",
    "this","it","on","at","is","was","we","you","please","make","create",
    "write","run","do","file","script","background","task","from","about",
    "earlier","today","yesterday","morning","just","some","which",
}


def _load():

    try:
        with open(FILE,encoding="utf-8") as f:
            data = json.load(f)
        return [d for d in data if isinstance(d,dict)]
    except (OSError,ValueError):
        return []


def _save(records):

    try:

        os.makedirs(os.path.dirname(FILE),exist_ok=True)

        temporario = FILE + ".tmp"

        with open(temporario,"w",encoding="utf-8") as f:
            json.dump(records,f,ensure_ascii=False,indent=1)

        os.replace(temporario,FILE)

    except OSError as e:
        print(f"[!] Não consegui salvar a memória de tarefas: {e}")


def register(id_task,request,state,response,files,steps=0):
    """Chamada no fim de cada tarefa (qualquer thread)."""

    record = {
        "id": id_task,
        "data": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "pedido": " ".join((request or "").split())[:300],
        "state": state,
        "resumo": " ".join((response or "").split())[:MAX_SUMMARY],
        "arquivos": sorted(set(files or []))[:20],
        "steps": steps,
    }

    with _trava:

        records = _load()
        records.append(record)
        _save(records[-MAX_RECORDS:])

    return record


def _words(text):
    return [
        p for p in re.findall(r"[a-z0-9_\-\.]+",(text or "").lower())
        if p not in STOPWORDS and len(p) > 1
    ]


def search(term="",limit=5):
    """
    Tarefas recentes que batem com o termo (palavras soltas, qualquer
    uma). Sem termo: as últimas. Mais novas primeiro.
    """

    with _trava:
        records = _load()

    if not records:
        return []

    keys = _words(term)

    if not keys:
        return list(reversed(records))[:limit]

    achados = []

    for r in reversed(records):

        target = " ".join([
            r.get("pedido",""),
            r.get("resumo",""),
            " ".join(os.path.basename(a) for a in r.get("arquivos",[])),
        ]).lower()

        checkpoints = sum(1 for c in keys if c in target)

        if checkpoints:
            achados.append((checkpoints,r))

    achados.sort(key=lambda x: -x[0])

    return [r for _,r in achados[:limit]]


def tool_query_tasks_recent(term=""):

    encontrados = search(term)

    if not encontrados:
        return {
            "success": True,
            "tarefas": [],
            "resultado": "No recent task matches that.",
        }

    return {
        "success": True,
        "tarefas": [
            {
                "quando": r.get("data",""),
                "pedido": r.get("pedido",""),
                "state": r.get("state",""),
                "resumo": r.get("resumo",""),
                "arquivos": r.get("arquivos",[]),
            }
            for r in encontrados
        ],
    }