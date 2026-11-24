"""
Rotina matinal da Lia: apresentação falada ao iniciar a sessão, com
cartões na tela sincronizados com a speech.

O modelo devolve o briefing em BLOCOS (saudação, clima, agenda de hoje,
agenda de amanhã, encerramento). Os blocos entram na fila de voz e cada
cartão aparece no instante em que o áudio do SEU bloco começa a tocar
(speech.py: ao_comecar). Os dados dos cartões não são inventados:
  - clima: vem direto da Open-Meteo (widgets.buscar_clima);
  - agenda: o modelo extrai os itens do texto do konsolekalendar, e o
    Python só aceita o item se o título aparecer MESMO nesse texto.

Ao terminar de falar a Lia volta a dormir (estado "sleeping") e os
cartões saem sozinhos, como em qualquer outra interação.
"""

import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import config
import llm
from calendar_tools import tool_query_calendars
from events import bus, emit
from widgets import search_weather
from memory import preferred_addresses, address_suffix


FILE_BOOT = (
    Path.home()
    / ".local/state/jarvisb/rotina_matinal_boot_id"
)

CLOSING_DEFAULT = (
    "I'm here whenever you need me. Just say Hey, Lia."
)

# How long to wait for the interface to connect before speaking.
WAIT_INTERFACE = 15.0

MAX_ITEMS_SCHEDULE = 8


def _query_schedule():
    try:
        # Two days: today and tomorrow.
        return tool_query_calendars(2)
    except Exception as e:
        return {"success": False, "error": str(e)}


def _query_weather():
    try:
        return search_weather()
    except Exception as e:
        return {"success": False, "error": str(e)}


INSTRUCAO = """
You are Lia, a personal voice assistant inspired by J.A.R.V.I.S.

Prepare a natural, concise spoken morning briefing for the moment
the computer session starts.

The briefing is split into BLOCKS. The blocks are spoken in order, and
the screen shows a card for a block at the moment that block starts.
So each block must talk ONLY about its own topic.

LANGUAGE AND PERSONALITY:
- Speak naturally in English, as if talking directly to the user.
- Be composed, intelligent, subtly witty, and personable.
- If forms of address are supplied in the context, you may choose one.
  If the list is empty, greet the user without a name or title.
- Do not force a title into every sentence.
- Avoid sounding robotic, overly formal, or like a checklist.

BLOCKS:
- "saudacao": greet the user according to the local time. Nothing
  else: no weather, no events.
- "weather": the weather, using only the supplied weather data. If no
  weather data is available, say briefly that you couldn't get it.
- "agenda_hoje": what is on the calendar today.
- "agenda_amanha": what is on the calendar tomorrow.
- "encerramento": let the user know you are available whenever needed
  and invite them to say "Hey, Lia" when they want your attention.

CALENDAR ITEMS ("items" inside agenda_hoje and agenda_amanha):
- They list the events of that day found in the supplied calendar
  text, so the screen can show them.
- Copy each title EXACTLY as written in the calendar text, in its
  original language. Never translate or rewrite a title.
- "time" is the start time as HH:MM (24-hour), or "" when the event has
  no time (all-day events and holidays).
- Include only events that really appear in the calendar text. Never
  invent events, times or titles.
- Do not repeat duplicates (for example, the same holiday listed in
  Portuguese and English: keep one).
- If the calendar could not be read, use an empty list and say briefly
  that you couldn't read it. Do not pretend that you accessed it.
- If there are no events on that day, use an empty list and say so
  naturally and briefly.

SPEECH:
- "fala" and the other texts are only the words to be spoken: no
  markdown, no bullet points, no explanation of your instructions.
- Aim for 4 to 8 natural sentences in total across all blocks. Adjust
  to the information available; do not add filler.

OUTPUT: return ONLY a JSON object, with no markdown fences, shaped like:
{
  "saudacao": "...",
  "weather": "...",
  "agenda_hoje": {"fala": "...", "items": [{"time": "09:00", "title": "..."}]},
  "agenda_amanha": {"fala": "...", "items": []},
  "encerramento": "..."
}
"""


def _extract_json(text):

    text = (text or "").strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)

    start = text.find("{")
    end = text.rfind("}")

    if start < 0 or end <= start:
        raise ValueError("The model did not return a JSON object.")

    return json.loads(text[start:end + 1])


def _generate_plan(schedule, weather, now):

    weather_for_model = (
        {k: v for k, v in weather["summary"].items() if k != "notice"}
        if weather.get("success") else weather
    )

    context = {
        "data_local": now.strftime("%Y-%m-%d"),
        "amanha": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
        "dia_da_semana": now.strftime("%A"),
        "hora_local": now.strftime("%H:%M"),
        "agenda_hoje_e_amanha": schedule,
        "previsao_do_tempo": weather_for_model,
        "forms_of_address": preferred_addresses(),
    }

    response = llm.GROQ_CLIENT.chat.completions.create(
        model=llm.GROQ_MODEL,
        messages=[
            {"role": "system", "content": INSTRUCAO},
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False)
            }
        ],
        temperature=0.7,
        max_tokens=1000,
        extra_body={
            "reasoning_effort": llm.GROQ_REASONING_EFFORT
        }
    )

    if not response.choices:
        raise RuntimeError("The model returned no introduction.")

    text = (response.choices[0].message.content or "").strip()

    if not text:
        raise RuntimeError("The introduction was empty.")

    plan = _extract_json(text)

    if not isinstance(plan, dict):
        raise ValueError("The briefing is not a JSON object.")

    return plan


def _norm(text):
    return " ".join(str(text).lower().split())


def _items_valid(items, text_raw):
    """
    Trava contra invenção: só passa o item cujo título aparece de fato no
    texto do calendário. Hora fora do formato HH:MM vira "" (dia inteiro).
    """

    base = _norm(text_raw)
    vistos = set()
    output = []

    for item in items or []:

        if not isinstance(item, dict):
            continue

        title = " ".join(str(item.get("title", "")).split())

        if not title or _norm(title) not in base:
            continue

        hora = str(item.get("time", "")).strip()

        if not re.fullmatch(r"\d{1,2}:\d{2}", hora):
            hora = ""

        key = (hora, _norm(title))

        if key in vistos:
            continue

        vistos.add(key)
        output.append({"time": hora, "title": title})

    output.sort(key=lambda i: i["time"].zfill(5))

    return output[:MAX_ITEMS_SCHEDULE]


def _text(value):
    return str(value or "").strip()


def _card_weather(weather):

    def show():
        emit(
            "widget",
            widget="weather",
            title=weather["city"],
            data=weather["data"]
        )

    return show


def _card_schedule(title, items):

    def show():
        emit(
            "widget",
            widget="schedule",
            title=title,
            data={"items": items}
        )

    return show


def _build_blocks(plan, schedule, weather):
    """Lista de (texto falado, função que mostra o cartão ou None)."""

    greeting = _text(plan.get("saudacao"))

    if not greeting:
        raise ValueError("The briefing has no greeting.")

    blocks = [(greeting, None)]

    # Weather: show a card only when real data is available.
    speech_weather = _text(plan.get("weather"))

    if speech_weather:
        blocks.append((
            speech_weather,
            _card_weather(weather) if weather.get("success") else None
        ))

    # Schedule: one card per day.
    schedule_ok = bool(schedule.get("success"))
    text_raw = _text(schedule.get("events")) if schedule_ok else ""
    without_events = text_raw.startswith("No events found")

    for key, title in (
        ("agenda_hoje", "Today"),
        ("agenda_amanha", "Tomorrow"),
    ):

        day = plan.get(key)

        if not isinstance(day, dict):
            continue

        speech = _text(day.get("fala"))

        if not speech:
            continue

        items = _items_valid(day.get("items"), text_raw) if schedule_ok else []

        # Show an empty card ("No events found") only when the calendar itself
        # reports no events. If reading failed or the model found no items
        # in non-empty text, show nothing.
        show = schedule_ok and (items or without_events)

        blocks.append((
            speech,
            _card_schedule(title, items) if show else None
        ))

    blocks.append((
        _text(plan.get("encerramento")) or CLOSING_DEFAULT,
        None
    ))

    return blocks


def _presentation_fallback(now):
    hora = now.hour

    if hora < 12:
        greeting = "Good morning"
    elif hora < 18:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    greeting += address_suffix()

    alternativas = [
        f"{greeting}. I couldn't prepare your full "
        "briefing this time, but I'm here whenever you need me. "
        "Just say Hey, Lia.",

        f"{greeting}. My calendar or weather update "
        "isn't fully available right now. Whenever you need anything, "
        "just say Hey, Lia.",

        f"{greeting}. I'm ready when you are. "
        "Just say Hey, Lia whenever you need me."
    ]

    return random.choice(alternativas)


def _wait_interface(limit=WAIT_INTERFACE):
    """
    Os cartões só aparecem se a interface já estiver conectada ao socket
    (o cliente novo só recebe o último estado, não os widgets perdidos).
    """

    if not (
        getattr(config, "ABRIR_INTERFACE_JUNTO", True)
        or getattr(config, "ABRIR_FUNDO_JUNTO", True)
    ):
        return

    if bus.has_clientes():
        return

    start = time.time()

    while not bus.has_clientes():

        if time.time() - start > limit:
            print("[rotina] Interface não conectou; falando sem cartões.")
            return

        time.sleep(0.2)

    # the secondary interface and QML finish loading
    time.sleep(1.5)


def _action_of_block(text, card, first):
    """O que a tela faz quando o áudio deste bloco começa."""

    def action():

        if first:
            emit("state", state="speaking")

        if card:
            card()

        # Caption below (the greeting does not need one).
        if not first:
            emit("response_end", text=text)

    return action


def _speak_blocks(voice, blocks):

    sincrono = getattr(voice, "suporta_sincronia", False)

    for i, (text, card) in enumerate(blocks):

        action = _action_of_block(text, card, i == 0)

        if sincrono:
            # queue everything; each card appears when its audio starts
            voice.speak(text, on_start=action)
        else:
            # unsynchronized voice: one block at a time, show the card immediately
            action()
            voice.speak(text)
            voice.wait_end()

    voice.wait_end()


def execute_routine_morning(voice):

    iniciou = False

    try:
        boot_id = Path(
            "/proc/sys/kernel/random/boot_id"
        ).read_text(encoding="utf-8").strip()

        FILE_BOOT.parent.mkdir(parents=True, exist_ok=True)

        if FILE_BOOT.exists():
            boot_previous = FILE_BOOT.read_text(
                encoding="utf-8"
            ).strip()

            if boot_previous == boot_id:
                print("[rotina] Apresentação já realizada neste boot.")
                return

        iniciou = True
        now = datetime.now()

        emit("state", state="thinking")

        print("[rotina] Consultando agenda e previsão do tempo...")

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_schedule = pool.submit(_query_schedule)
            f_weather = pool.submit(_query_weather)
            schedule = f_schedule.result()
            weather = f_weather.result()

        try:
            plan = _generate_plan(schedule, weather, now)
            blocks = _build_blocks(plan, schedule, weather)
        except Exception as e:
            print(f"[rotina] Não foi possível gerar o briefing: {e}")
            blocks = [(_presentation_fallback(now), None)]

        for text, card in blocks:
            print(f"[rotina] {'[cartão] ' if card else ''}{text}")

        _wait_interface()

        _speak_blocks(voice, blocks)

        FILE_BOOT.write_text(
            boot_id,
            encoding="utf-8"
        )

        print("[rotina] Apresentação concluída.")

    except Exception as e:
        print(f"[rotina] Erro na apresentação inicial: {e}")

    finally:
        # The briefing is over: Lia goes back to sleep and the cards
        # disappear on their own (as in any other interaction).
        if iniciou:
            emit("state", state="sleeping")