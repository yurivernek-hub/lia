"""
Demonstração da interface SEM o Jarvis de voz (sem microfone, Whisper ou
Groq): simula despertar, escuta, widgets de clima e notícias, resposta em
texto e volta ao IDLE. Terminal 1:  python demo_interface.py
Terminal 2 (depois): python interface/jarvis_ui.py --environment-always
                     ou o fundo em C++:  background/build/jarvisb-background
"""

import math
import random
import time

from events import bus,emit

if not bus.start():
    raise SystemExit(1)

print(f"Socket: {bus.path} — abra a interface em outro terminal.")

WEATHER = {
    "temp": 26.7,"feels_like": 29.1,"humidity": 82,"wind": 18.3,
    "description": "Partly cloudy","icon": "⛅",
    "min": 23.3,"max": 27.5,"rain": 62,
    "days": [
        {"day": "Today","min": 23.3,"max": 27.5,"rain": 62,"description": "Partly cloudy","icon": "⛅"},
        {"day": "Tomorrow","min": 23.8,"max": 28,"rain": 40,"description": "Rain","icon": "☂"},
        {"day": "Fri","min": 23,"max": 27.1,"rain": 10,"description": "Overcast","icon": "☁"},
    ],
}

NEWS = {
    "items": [
        {"headline": "OpenAI freia lançamento de novo modelo diante de riscos de segurança","source": "Veja"},
        {"headline": "Nova pesquisa Atlas Intel mostra a disputa a cinco dias da eleição","source": "CNN Brasil"},
        {"headline": "Big techs ampliam investimentos em data centers de IA","source": "Valor Econômico"},
    ]
}


def listen(seconds):

    emit("state",state="listening")

    for i in range(int(seconds / 0.064)):
        emit("level",value=min(1.0,abs(math.sin(i / 5)) * random.uniform(0.4,1.0)))
        time.sleep(0.064)


def speak(phrase):

    emit("state",state="speaking")

    text = ""

    for word in phrase.split():

        text += word + " "
        emit("partial_response",text=word + " ")

        for _ in range(4):
            emit("level",value=random.uniform(0.1,0.9))
            time.sleep(0.06)

    emit("response_end",text=text.strip())
    emit("level",value=0.0)


def interaction(question,widget,title,data,speech):

    listen(2)
    emit("state",state="transcribing")
    time.sleep(0.6)
    emit("user_speech",text=question)
    emit("state",state="thinking")
    time.sleep(2.2)

    if widget:
        emit("widget",widget=widget,title=title,data=data)

    speak(speech)
    time.sleep(1.5)
    emit("state",state="listening")


try:

    while True:

        emit("state",state="sleeping")
        time.sleep(5)

        emit("state",state="awakened")
        time.sleep(1)

        interaction(
            "What's the weather like?","weather","Maragogi",WEATHER,
            "It's 26 degrees and partly cloudy, with a 62 percent chance of rain today."
        )

        interaction(
            "Show me the tech news.","news","Technology",NEWS,
            "OpenAI has paused the launch of a new model over safety concerns."
        )

        interaction(
            "Tell me something interesting.",None,"",{},
            "Octopuses have three hearts, and two of them stop when they swim."
        )

        emit("state",state="sleeping")
        time.sleep(8)

except KeyboardInterrupt:
    pass

finally:
    bus.stop()