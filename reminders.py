"""
Lembretes, alarmes e avisos de reunião da Lia.

Três types, todos na mesma fila de voz (voice_control.py):
  - Lembrete pontual: "remind me in 30 minutes to stretch", ou "às 15:00".
  - Pré-aviso: "warn me 15 minutes before"; o aviso sai antes do horário.
  - Agenda: a cada 30 min lê os compromissos de hoje e cria pré-avisos
    automáticos (15 min antes). Os títulos passam pela mesma trava do
    rotina_matinal: só vale o título que aparece no texto do calendário.

Persistência: ~/.local/state/jarvisb/reminders.json. Um lembrete que
passou do horário enquanto a Lia estava desligada é anunciado ao iniciar
(com a frase de atraso), em vez de sumir.

Só usa biblioteca padrão no topo. Dependências pesadas (ferramentas,
llm) são importadas dentro das funções, então o módulo carrega em
qualquer teste.
"""

import datetime as _dt
import json
import os
import re
import threading
import time

import personality

FILE = os.path.expanduser("~/.local/state/jarvisb/reminders.json")
INTERVALO_SCHEDULE = 30 * 60          # seconds between calendar reads
ADVANCE_NOTICE_SCHEDULE = 15            # minutes
MAX_ITEMS_SCHEDULE = 15
MAX_REMINDERS = 100

_trava = threading.RLock()
_reminders = []
_announce = None
_search_schedule = None
_next_id = 1
_last_schedule = 0.0
_iniciado = False


def _now():
    """Hora local (separado para os testes)."""
    return _dt.datetime.now()


def _to_text(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _from_text(s):
    return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")


# ----------------------------------------------------------------------
# persistência
# ----------------------------------------------------------------------

def _load():
    global _reminders, _next_id

    try:
        with open(FILE,encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = []

    _reminders = [d for d in data if isinstance(d,dict) and "quando" in d]
    _next_id = max([d.get("id",0) for d in _reminders] + [0]) + 1


def _save():

    try:

        os.makedirs(os.path.dirname(FILE),exist_ok=True)

        temporario = FILE + ".tmp"

        with open(temporario,"w",encoding="utf-8") as f:
            json.dump(_reminders[-MAX_REMINDERS:],f,ensure_ascii=False,indent=1)

        os.replace(temporario,FILE)

    except OSError as e:
        print(f"[!] Não consegui salvar os lembretes: {e}")


# ----------------------------------------------------------------------
# Internal API
# ----------------------------------------------------------------------

def start(announce,search_schedule=None):
    """
    Chamado no boot (jarvisb.py). anunciar: controle.anunciar.
    buscar_agenda: função sem argumentos que devolve os compromissos de
    hoje como [{"time": "HH:MM" ou "", "title": "..."}], ou None se
    não conseguiu ler (nesse caso nada é alterado).
    """

    global _announce, _search_schedule, _iniciado

    with _trava:

        if _iniciado:
            return

        _iniciado = True
        _announce = announce
        _search_schedule = search_schedule

        _load()
        _notify_late()

    threading.Thread(target=_loop,daemon=True,name="lembretes").start()


def create(message,when,advance_notice=0,origin="usuario",key=None):

    global _next_id

    with _trava:

        item = {
            "id": _next_id,
            "message": message,
            "quando": _to_text(when),
            "antecedencia": int(advance_notice),
            "origem": origin,
            "chave": key,
            "pre_feito": False,
            "feito": False,
        }

        _next_id += 1
        _reminders.append(item)
        _save()

        return dict(item)


def pending():

    with _trava:
        return [dict(d) for d in _reminders if not d["feito"]]


def cancel(id_):

    with _trava:

        antes = len(_reminders)
        _reminders[:] = [d for d in _reminders if d["id"] != id_]

        if len(_reminders) != antes:
            _save()
            return True

        return False


def _notify_late():
    """Lembretes que venciam enquanto a Lia estava desligada."""

    now = _now()

    for d in list(_reminders):

        if d["feito"]:
            continue

        if _from_text(d["quando"]) <= now:
            _fire(d,late=True)


def _fire(d,late=False):

    d["feito"] = True
    d["pre_feito"] = True
    _save()

    if late:
        text = f"I'm late with this one: {d['message']}. I was switched off, which is no excuse, but there it is."
    else:
        text = personality.phrase("lembrete",msg=d["message"])

    _send(text,"Reminder")


def _pre_notice(d):

    d["pre_feito"] = True
    _save()

    text = personality.phrase(
        "pre_aviso",
        msg=d["message"],
        min=d["antecedencia"]
    )

    _send(text,"Upcoming")


def _send(text,title):

    if _announce is None:
        print(f"\n[⏰ {title}] {text}")
        return

    try:
        _announce(
            text=text,
            origin="lembrete",
            type="notice",
            prioridade=1,
            title=title,
        )
    except Exception as e:
        print(f"[!] Erro ao anunciar lembrete: {e}")


def verify(now=None):
    """Um ciclo: dispara o que venceu e os pré-avisos. Usado pelo laço e pelos testes."""

    now = now or _now()

    with _trava:

        for d in list(_reminders):

            if d["feito"]:
                continue

            when = _from_text(d["quando"])

            if (
                d["antecedencia"] > 0
                and not d["pre_feito"]
                and now >= when - _dt.timedelta(minutes=d["antecedencia"])
                and now < when
            ):
                _pre_notice(d)

            if now >= when:
                _fire(d)


def synchronize_schedule(items,now=None):
    """
    Cria pré-avisos para os compromissos de hoje, e remove os de hoje que
    saíram da agenda. Itens sem hora (dia inteiro) não geram aviso.
    """

    now = now or _now()
    today = now.date()

    with _trava:

        keys_novas = set()

        for item in items or []:

            hora = (item.get("time") or "").strip()
            title = " ".join((item.get("title") or "").split())

            if not hora or not title:
                continue

            try:
                h,m = [int(x) for x in hora.split(":")]
                when = _dt.datetime.combine(today,_dt.time(h,m))
            except ValueError:
                continue

            if when <= now:
                continue

            key = f"{today.isoformat()}|{hora}|{title.lower()}"
            keys_novas.add(key)

            ja_exists = any(d.get("chave") == key for d in _reminders)

            if not ja_exists:
                create(
                    f"{title} at {hora}",
                    when,
                    advance_notice=ADVANCE_NOTICE_SCHEDULE,
                    origin="schedule",
                    key=key,
                )

        antes = len(_reminders)
        _reminders[:] = [
            d for d in _reminders
            if not (
                d.get("origem") == "schedule"
                and not d["feito"]
                and d.get("chave","").startswith(today.isoformat())
                and d.get("chave") not in keys_novas
            )
        ]

        if len(_reminders) != antes:
            _save()


def _loop():

    global _last_schedule

    while True:

        try:

            verify()

            if (
                _search_schedule is not None
                and time.time() - _last_schedule >= INTERVALO_SCHEDULE
            ):

                _last_schedule = time.time()

                items = _search_schedule()

                if items is not None:
                    synchronize_schedule(items)

        except Exception as e:
            print(f"[!] Erro no laço de lembretes: {e}")

        time.sleep(1)


# ----------------------------------------------------------------------
# today’s schedule: calendar lookup plus an anti-hallucination check
# ----------------------------------------------------------------------

def _norm(text):
    return " ".join(str(text).lower().split())


def validate_items_schedule(items,text_raw):
    """Só passa item cujo título aparece no texto do calendário."""

    base = _norm(text_raw)
    vistos = set()
    output = []

    for item in items or []:

        if not isinstance(item,dict):
            continue

        title = " ".join(str(item.get("title","")).split())

        if not title or _norm(title) not in base:
            continue

        hora = str(item.get("time","")).strip()

        if not re.fullmatch(r"\d{1,2}:\d{2}",hora):
            hora = ""

        key = (hora,_norm(title))

        if key in vistos:
            continue

        vistos.add(key)
        output.append({"time": hora,"title": title})

    output.sort(key=lambda i: i["time"].zfill(5))

    return output[:MAX_ITEMS_SCHEDULE]


def search_schedule_today():
    """
    Lê o calendário de hoje e extrai os compromissos com o modelo. Devolve
    None se não conseguiu ler (nada é alterado).
    """

    try:

        import json as _json

        import tools
        import llm

        raw = tools.tool_query_calendars(1)

        if not raw.get("success"):
            return None

        # Same field read by morning_routine ("events"); it may be a list.
        text = raw.get("events") or ""
        if isinstance(text,list):
            text = "\n".join(str(x) for x in text)

        if not text.strip():
            return []

        pacote = llm.GROQ_CLIENT.chat.completions.create(
            model=llm.GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract only the events of today from this calendar "
                        "text. Reply with ONLY a JSON list, no prose: "
                        '[{"time": "HH:MM" or "" for all-day, "title": "..."}]. '
                        "Use the exact title as written."
                    ),
                },
                {"role": "user","content": text[:4000]},
            ],
            temperature=0,
            max_tokens=600,
        )

        raw_model = pacote.choices[0].message.content or ""

        ini,end = raw_model.find("["),raw_model.rfind("]")

        if ini < 0 or end < ini:
            return None

        items = _json.loads(raw_model[ini:end+1])

        return validate_items_schedule(items,text)

    except Exception as e:
        print(f"[!] Não consegui ler a agenda: {e}")
        return None


# ----------------------------------------------------------------------
# Tools for the model
# ----------------------------------------------------------------------

def _schedule_time_next(hhmm,now):

    h,m = [int(x) for x in hhmm.split(":")]

    when = _dt.datetime.combine(now.date(),_dt.time(h,m))

    if when <= now:
        when += _dt.timedelta(days=1)

    return when


def tool_create_reminder(message="",minutes=0,schedule_time="",advance_notice_minutes=0):

    message = " ".join(str(message).split())

    if not message:
        return {"success": False,"error": "The reminder needs a message."}

    now = _now()

    try:
        minutes = float(minutes or 0)
        advance_notice = int(advance_notice_minutes or 0)
    except (TypeError,ValueError):
        return {"success": False,"error": "The time must be a number of minutes."}

    if minutes > 0:
        when = now + _dt.timedelta(minutes=minutes)
    elif re.fullmatch(r"\d{1,2}:\d{2}",str(schedule_time or "")):
        when = _schedule_time_next(str(schedule_time),now)
    else:
        return {
            "success": False,
            "error": "Give either minutes from now, or a time as HH:MM."
        }

    if advance_notice < 0 or advance_notice >= (when - now).total_seconds() / 60:
        advance_notice = 0

    item = create(message,when,advance_notice=advance_notice)

    return {
        "success": True,
        "id": item["id"],
        "quando": when.strftime("%H:%M on %a %d/%m"),
        "result": f"Reminder {item['id']} set for {when.strftime('%H:%M')}.",
    }


def tool_list_reminders():

    items = pending()

    return {
        "success": True,
        "lembretes": [
            {
                "id": d["id"],
                "quando": d["quando"].replace("T"," ")[:16],
                "message": d["message"],
            }
            for d in items
        ],
    }


def tool_cancel_reminder(id=0):

    try:
        id_ = int(id)
    except (TypeError,ValueError):
        return {"success": False,"error": "Give the reminder number."}

    if cancel(id_):
        return {"success": True,"result": f"Reminder {id_} cancelled."}

    return {"success": False,"error": f"There is no reminder number {id_}."}