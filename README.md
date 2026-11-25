# LIA

**LIA** is a voice-controlled desktop assistant for Linux created by Yuri Vernek. Say the wake word, speak naturally, and LIA listens, thinks, talks back, and can act on your system: open apps, manage files, run commands, search the web, and show the weather or the news right on your desktop.

It was built to feel like a real assistant living in your computer rather than a chat window: a live animated background reacts to your voice, a small floating panel lets you type or confirm actions, and everything happens hands-free.

> **Platform:** Linux only (KDE Plasma 6 with KWin on Wayland).

## Features

- **Wake word activation**: hands-free "hey Lia" detection using [openWakeWord](https://github.com/dscripka/openWakeWord).
- **Speech-to-text**: transcribes your commands, in the cloud or locally.
- **Conversational AI**: streaming responses with short-term conversation history.
- **Text-to-speech**: natural offline voice powered by [Piper](https://github.com/rhasspy/piper).
- **Tool use**: the assistant can
  - open applications,
  - create, read and list files inside a sandboxed workspace,
  - run shell commands,
  - search the web,
  - show weather forecasts and news headlines.
- **Safe by design**: actions that change your system ask for confirmation first, by voice or with a button. File access is restricted to allowed folders, and `sudo` is never used.
- **Long-term memory**: LIA can recognize durable preferences in conversation and retrieve saved facts by topic. You can also explicitly save, list, correct and delete facts.
- **Desktop integration**: an animated, voice-reactive wallpaper layer (C++/Qt6 with Layer Shell) and a floating panel (PySide6/QML) that positions itself intelligently across monitors through a KWin script.

## Run it your way

LIA supports two modes:

| Mode | Language model and speech recognition | Best for |
| --- | --- | --- |
| **API** | [Groq](https://groq.com) (chat, tool calling, Whisper) | Fast responses on any hardware |
| **Local** | Models running on your own machine (e.g. via [Ollama](https://ollama.com)) | Privacy and offline use |

Text-to-speech and wake word detection always run locally.

## How it works

LIA is split into independent processes that talk to each other over a local UNIX socket and D-Bus:

```
 microphone ──► core (Python) ──── UNIX socket ────► floating panel (PySide6 + QML)
                  │                      └─────────► desktop background (C++ / Qt6 + QML)
                  ├─► LLM + tools (Groq API or local model)
                  └─► Piper TTS ──► speakers

 KWin script ──── D-Bus ────► panel + background (window placement and monitor awareness)
```

| Component | Technology | Role |
| --- | --- | --- |
| Core (`jarvisb.py`) | Python 3 | Wake word, microphone, speech-to-text, LLM, tools, voice |
| Panel (`interface/`) | Python, PySide6, QML | Floating window to type, confirm actions and see responses |
| Background (`background/`) | C++, Qt6, QML, LayerShellQt | Animated desktop layer with an orb and widgets |
| KWin script | JavaScript (KWin 6) | Places the panel and detects free or busy monitors |

## Requirements

- Linux with KDE Plasma 6 (KWin) on Wayland
- Python 3.10+
- Qt 6, PySide6 and LayerShellQt
- CMake and a C++ compiler (to build the background)
- A working microphone and speakers
- [Piper](https://github.com/rhasspy/piper) with a voice model
- Python packages: `openwakeword`, `pyaudio`, `PySide6`, `groq` (API mode)
- A [Groq API key](https://console.groq.com) for API mode, or a local model runtime for local mode

## Getting started

```bash
git clone https://github.com/yurivernek-hub/LIA.git
cd LIA

# Build the desktop background
cmake -S background -B background/build
cmake --build background/build

# Run the assistant
python jarvisb.py
```

On first launch LIA asks for your Groq API key (API mode) and stores it in `~/.config/jarvisb/env` with restricted permissions. You can also provide it through the `GROQ_API_KEY` environment variable.

## Configuration

On first launch LIA creates a private copy of [settings.example.json](settings.example.json) at `~/.config/jarvisb/settings.json`. Edit that copy and restart LIA. You can set your name, preferred form of address, weather city, news region and speech recognition language. Leave the city blank to specify it when asking about the weather. A form of address saved during conversation takes precedence in the conversation prompt.

LIA stores personal memories in `~/.config/jarvisb/memories.json` and the learned profile in `~/.config/jarvisb/profile.json`. On upgrade it imports the previous project-local JSON files when the corresponding private file is missing. The originals remain as ignored backups. The API key stays in the separate private `env` file. Model, voice and desktop defaults are in `config.py`.

### Personal memory

In the default `auto` mode, LIA sends the full current message and recent conversation context to Groq for memory analysis. One pass extracts possible facts, and a second checks their owner, meaning, durability and permission against the same conversation. For a bare name or place, LIA also checks whom the preceding question was about before retaining the answer. “I live in Recife” can update your city immediately. If you tell LIA that someone else said those same words, their city does not belong in your profile. Ambiguous claims are skipped. In `ask` mode you can accept the proposed facts together in your next reply.

Set `memory_mode` in your private settings to `ask` to confirm every suggestion or `manual` to disable automatic analysis. Explicit commands work in all three modes, by voice or text:

- “Remember that I play the cello” or “Lembre-se que eu toco violoncelo”.
- “List my memories” or “Liste minhas memórias” to see the numbered facts.
- “Correct memory 2 to I play the violin” or “Corrija a memória 2 para eu toco violino”.
- “Delete memory 2” or “Esqueça que eu toco violino”.

The version 2 JSON groups name, home city, school and preferred form of address under `person`. Each field is either null or an object containing `value`, `id`, `date`, `source` and `topics`. Other accepted facts go into the categorized `memories` list. A new home city replaces the old value under the same number. Old lists are still readable and are upgraded on the next write without guessing which facts describe the user. Chat and task prompts receive up to three matching facts. Listing memories reads the private file directly. Editing or deleting a fact clears the current chat history so the old text is not reused in the next turn. Older project-local backups are separate from the active memory file.

The local regression suite runs with `python -m unittest discover -s tests`. To evaluate speaker attribution using the configured Groq model, run `LIA_LIVE_MEMORY_TESTS=1 python -m unittest discover -s tests -p test_memory_semantics.py` in the LIA environment. The live evaluation sends synthetic conversations and uses API quota.

## Usage examples

- *"Hey Lia, open Firefox."*
- *"What's the weather like today?"*
- *"Show me the latest news about technology."*
- *"Create a file called notes.txt with my shopping list."*
- *"Remember that my sister's birthday is in March."*

## Project structure

```
.
├── jarvisb.py             # Entry point: wake word, listen, respond
├── config.py              # Runtime settings
├── llm.py                 # Language model and conversation history
├── stt.py                 # Speech recognition
├── speech.py              # Text-to-speech
├── microphone.py          # Audio capture and voice activity
├── tools.py               # Tool definitions and workspace sandbox
├── tool_flow.py           # Tool calling and confirmation
├── task.py                # Multi-step task execution
├── background_tasks.py    # Background task manager
├── memory.py              # Long-term memory and user profile
├── calendar_tools.py      # Calendar integration
├── web_search.py          # Web search integration
├── reminders.py           # Reminder scheduling
├── interface/             # Floating panel and KWin integration
├── background/            # C++ desktop background
└── wakeword/              # Wake word model
```
