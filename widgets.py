"""
Widgets do Jarvis: ferramentas que buscam dados (clima, notícias) e os
MOSTRAM na tela como cartões, além de devolver um resumo pro modelo falar.

O cartão viaja pelo barramento de eventos:
    {"event": "widget", "widget": "weather"|"news"|"schedule", "title": ..., "data": {...}}
e quem desenha é a interface (background/EnvironmentScene.qml). O cartão "schedule" é
emitido pela morning_routine.py (não é uma ferramenta).

Fontes (sem chave de API):
  - clima: Open-Meteo (geocodificação + previsão)
  - notícias: RSS do Google Notícias
"""

import datetime
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from config import CITY_DEFAULT,NEWS_REGION
from events import emit

TIMEOUT = 20

DAYS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

# WMO weather codes mapped to descriptions and icons.
CODIGOS_WEATHER = {
    0: ("Clear sky","☀"),
    1: ("Mostly clear","☀"),
    2: ("Partly cloudy","⛅"),
    3: ("Overcast","☁"),
    45: ("Fog","☁"),
    48: ("Fog","☁"),
    51: ("Light drizzle","☂"),
    53: ("Drizzle","☂"),
    55: ("Heavy drizzle","☂"),
    56: ("Freezing drizzle","☂"),
    57: ("Freezing drizzle","☂"),
    61: ("Light rain","☂"),
    63: ("Rain","☂"),
    65: ("Heavy rain","☂"),
    66: ("Freezing rain","☂"),
    67: ("Freezing rain","☂"),
    71: ("Light snow","❄"),
    73: ("Snow","❄"),
    75: ("Heavy snow","❄"),
    77: ("Snow grains","❄"),
    80: ("Rain showers","☂"),
    81: ("Rain showers","☂"),
    82: ("Violent showers","☂"),
    85: ("Snow showers","❄"),
    86: ("Snow showers","❄"),
    95: ("Thunderstorm","⚡"),
    96: ("Thunderstorm with hail","⚡"),
    99: ("Thunderstorm with hail","⚡"),
}

# region -> (hl, gl, ceid) for Google News
REGIOES_NEWS = {
    "pt-BR": ("pt-BR","BR","BR:pt-419"),
    "en-US": ("en-US","US","US:en"),
}


def _download(url):

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "JarvisB/1.0"}
    )

    with urllib.request.urlopen(request,timeout=TIMEOUT) as r:
        return r.read()


def _json(url):
    return json.loads(_download(url).decode("utf-8"))


def _number(value,decimals=1):

    try:
        number = round(float(value),decimals)
    except (TypeError,ValueError):
        return None

    return int(number) if number == int(number) else number


def search_weather(city=""):
    """
    Só BUSCA os dados (não emite nada na tela). Devolve
    {"success": True, "city", "data" (pro cartão), "summary" (pro modelo)}
    ou {"success": False, "error"}.
    """


    city = (city or CITY_DEFAULT or "").strip()

    if not city:
        return {"success": False,"error": "No city was given."}

    try:

        geo = _json(
            "https://geocoding-api.open-meteo.com/v1/search?"
            + urllib.parse.urlencode({
                "name": city,
                "count": 1,
                "language": "en",
                "format": "json",
            })
        )

        achados = geo.get("results") or []

        if not achados:
            return {
                "success": False,
                "error": f"I couldn't find a city called {city}."
            }

        lugar = achados[0]

        forecast = _json(
            "https://api.open-meteo.com/v1/forecast?"
            + urllib.parse.urlencode({
                "latitude": lugar["latitude"],
                "longitude": lugar["longitude"],
                "current": (
                    "temperature_2m,relative_humidity_2m,"
                    "apparent_temperature,weather_code,wind_speed_10m"
                ),
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max"
                ),
                "timezone": "auto",
                "forecast_days": 3,
            })
        )

        current = forecast["current"]
        diario = forecast["daily"]

    except (urllib.error.URLError,KeyError,ValueError,OSError) as e:

        return {"success": False,"error": f"I couldn't get the weather: {e}"}

    name = lugar.get("name") or city

    description,icone = CODIGOS_WEATHER.get(
        current.get("weather_code"),
        ("Unknown","☁")
    )

    days = []

    for i,data in enumerate(diario.get("time",[])):

        if i >= 3:
            break

        try:
            name_day = "Today" if i == 0 else (
                "Tomorrow" if i == 1 else DAYS[
                    datetime.date.fromisoformat(data).weekday()
                ]
            )
        except ValueError:
            name_day = data

        text_day,icone_day = CODIGOS_WEATHER.get(
            diario["weather_code"][i],("Unknown","☁")
        )

        days.append({
            "day": name_day,
            "min": _number(diario["temperature_2m_min"][i]),
            "max": _number(diario["temperature_2m_max"][i]),
            "rain": _number(diario["precipitation_probability_max"][i],0),
            "description": text_day,
            "icon": icone_day,
        })

    data = {
        "temp": _number(current.get("temperature_2m")),
        "feels_like": _number(current.get("apparent_temperature")),
        "humidity": _number(current.get("relative_humidity_2m"),0),
        "wind": _number(current.get("wind_speed_10m")),
        "description": description,
        "icon": icone,
        "min": days[0]["min"] if days else None,
        "max": days[0]["max"] if days else None,
        "rain": days[0]["rain"] if days else None,
        "days": days,
    }

    # Summary for the model to speak (the card already shows all the numbers).
    summary = {
        "success": True,
        "city": name,
        "current": {
            "temperature_c": data["temp"],
            "feels_like_c": data["feels_like"],
            "condition": description,
            "humidity_pct": data["humidity"],
            "wind_kmh": data["wind"],
        },
        "forecast": [
            {
                "day": d["day"],
                "min_c": d["min"],
                "max_c": d["max"],
                "chance_of_rain_pct": d["rain"],
                "condition": d["description"],
            }
            for d in days
        ],
        "notice": "O cartão do clima já está na tela do usuário.",
    }

    return {"success": True,"city": name,"data": data,"summary": summary}


def tool_show_weather(city=""):

    r = search_weather(city)

    if not r.get("success"):
        return r

    emit("widget",widget="weather",title=r["city"],data=r["data"])

    return r["summary"]


def tool_show_news(topic=""):

    topic = (topic or "").strip()
    hl,gl,ceid = REGIOES_NEWS.get(NEWS_REGION,REGIOES_NEWS["en-US"])

    parametros = {"hl": hl,"gl": gl,"ceid": ceid}

    if topic:
        url = (
            "https://news.google.com/rss/search?"
            + urllib.parse.urlencode({"q": topic,**parametros})
        )
    else:
        url = "https://news.google.com/rss?" + urllib.parse.urlencode(parametros)

    try:
        root = ET.fromstring(_download(url))
    except (urllib.error.URLError,ET.ParseError,OSError) as e:
        return {"success": False,"error": f"I couldn't get the news: {e}"}

    items = []

    for item in root.iter("item"):

        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()

        # Titles use "Headline - Source"; remove the repeated source suffix.
        sufixo = f" - {source}"

        if source and title.endswith(sufixo):
            title = title[: -len(sufixo)].strip()

        if not title:
            continue

        items.append({
            "headline": title,
            "source": source,
            "link": (item.findtext("link") or "").strip(),
        })

        if len(items) >= 4:
            break

    if not items:
        return {"success": False,"error": "The news search returned nothing."}

    emit(
        "widget",
        widget="news",
        title=topic or "Top stories",
        data={"items": items}
    )

    return {
        "success": True,
        "topic": topic or "top stories",
        "headlines": [
            {"headline": i["headline"],"source": i["source"]} for i in items
        ],
        "notice": (
            "O cartão de notícias já está na tela do usuário. As manchetes "
            "estão em português: resuma em inglês, sem ler todas."
        ),
    }