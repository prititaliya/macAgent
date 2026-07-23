# MacAgent

[![Release](https://img.shields.io/github/v/release/prititaliya/macAgent?label=download)](https://github.com/prititaliya/macAgent/releases/latest)
[![CI](https://github.com/prititaliya/macAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/prititaliya/macAgent/actions/workflows/ci.yml)

**A local macOS overlay agent** — press a shortcut, ask in plain English (type or speak), and it *does things on your Mac*: opens apps, finds and deletes files, searches the web, controls the UI, and asks you to **Approve** before anything destructive.

Runs entirely on your machine (local LLM + Python tools). Not a cloud chatbot pasted into a window.

---

## Download

| | |
|---|---|
| **macOS app** | [**Download MacAgent.dmg**](https://github.com/prititaliya/macAgent/releases/latest/download/MacAgent.dmg) |
| **Requirements** | macOS 13+, Apple Silicon (M1/M2/M3/M4) |
| **Latest releases** | [github.com/prititaliya/macAgent/releases](https://github.com/prititaliya/macAgent/releases) |

The DMG contains the menu-bar overlay app. You also need the Python backend (one-time setup below) — the app auto-starts it when you summon the overlay.

---

## Setup

### 1. Install the overlay app

1. Download [**MacAgent.dmg**](https://github.com/prititaliya/macAgent/releases/latest/download/MacAgent.dmg).
2. Open the DMG and drag **MacAgent.app** into **Applications**.
3. Launch MacAgent (sparkles icon in the menu bar).

### 2. Set up the Python backend

The brain of MacAgent is a local FastAPI daemon. Clone the repo to `~/MacAgent` and run the setup script:

```bash
git clone https://github.com/prititaliya/macAgent.git ~/MacAgent
cd ~/MacAgent
./automation/setup_backend.sh
```

This script will:

- Install Homebrew deps (`espeak-ng`, `portaudio`, `libsndfile`) for voice
- Create a Python virtualenv and install packages (with Metal-accelerated `llama-cpp-python`)
- Download the default **Qwen3-4B** GGUF model (~2.3 GB) to `~/Models/`
- Update `config/settings.json` with your model path

**Manual setup** (if you prefer):

```bash
brew install espeak-ng portaudio libsndfile
cd ~/MacAgent
python3 -m venv venv && source venv/bin/activate
CMAKE_ARGS="-DGGML_METAL=on -DCMAKE_OSX_ARCHITECTURES=arm64" ARCHFLAGS="-arch arm64" \
  pip install -r requirements.txt
```

Download a GGUF model and set `model_path` in `config/settings.json`. Default recommendation:

`~/Models/Qwen3-4B-Q4_K_M.gguf` — [Qwen3-4B Q4_K_M](https://huggingface.co/Qwen/Qwen3-4B-GGUF)

On 8GB Macs, prefer Qwen3-4B/8B. Qwen3-30B-A3B GGUFs need ~10GB+ free disk and typically 16GB+ unified memory.

### 3. Grant permissions

| Permission | Why | When |
|---|---|---|
| **Accessibility** | Global hotkey + UI click/type | First launch — enable **MacAgent.app**, not AEServer |
| **Microphone** | In-overlay dictation | First mic tap |
| **Speech Recognition** | On-device speech-to-text | First mic tap |
| **Full Disk Access** *(optional)* | Better file search via Spotlight | System Settings → Privacy |

### 4. Use it

- **⌃⌥Space** (Control-Option-Space) — show/hide the floating overlay (top-right corner)
- Type a question or tap the **mic** to dictate
- Menu bar icon → **Preferences** (sites, apps, notes, history, auto-hide)
- Toggle voice output in Preferences → Settings

Spoken status + answers use [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (downloaded on first use).

---

## What it can do

| Capability | Example asks |
|---|---|
| **Answer + act in a loop** | Multi-step goals: find → act → confirm |
| **Files & Downloads** | “What's my latest download?” · “Delete the file I downloaded last” *(Approve first)* |
| **Apps & Chrome** | “Open Slack” · “Open https://github.com in Chrome” |
| **Web research** | “Compare frontier LLM models and their prices” — searches, scrapes, retries if thin |
| **System power / Trash** | “Empty the bin” · “Shut down my Mac” — always **Approve / Deny** first |
| **Screen control** | Click, type, menus via Accessibility |
| **Shell & Python** | Short local bash / Python for compute and file tasks |
| **Personal memory** | Notes in Preferences (“what do you know about me?”) |
| **Voice** | Mic in the overlay **or** FreeFlow → same agent |

### Safety

Destructive actions (`rm`, empty Trash, shut down, restart, sleep) never run silently. The overlay shows an **Approve / Deny** card with the exact command.

---

## Demo (2 minutes)

1. Launch MacAgent → **⌃⌥Space** to show the overlay.
2. **Ask:** `what's my latest download?` → names the newest file in Downloads.
3. **Ask:** `delete the file I downloaded latest` → Approve card → Approve → file gone.
4. **Ask:** `compare frontier LLM models and their prices` → watch activity logs: search → retry → answer.
5. **Ask:** `open Slack` → app launches.
6. **Ask:** `yo` → friendly reply, no tools.

---

## Architecture

```text
  You (⌃⌥Space overlay / mic / FreeFlow)
              │
              ▼
     Local FastAPI daemon (:8081)
              │
              ▼
   Multi-step agent loop (local Qwen)
      ├─ web_search  (DuckDuckGo + page scrape)
      ├─ run_bash / run_python
      ├─ open_app / open_url / Settings
      ├─ ui_click / ui_type / …
      └─ respond  (only when the goal is done)
              │
              ▼
   Overlay: answer · sources · Approve card · activity logs
```

Everything stays on-device except outbound web search when needed.

---

## FreeFlow (optional voice)

Point FreeFlow's LLM base URL at MacAgent while the daemon is running:

1. STT as usual (e.g. Groq).
2. Post-processing / LLM base URL: `http://127.0.0.1:8081/v1`
3. Keep MacAgent running while you dictate.

```bash
defaults write com.zachlatta.freeflow post_processing_timeout_seconds -float 120
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Overlay shows “Starting daemon…” forever | Run `./automation/setup_backend.sh` and confirm `~/MacAgent/venv` exists |
| Daemon won't start | Check `~/Library/Logs/MacAgent/daemon.err` |
| Model errors | Verify `model_path` in `config/settings.json` points to a valid `.gguf` file |
| Hotkey doesn't work | System Settings → Privacy → Accessibility → enable **MacAgent** |
| Rebuild overlay from source | `./automation/open_macagent.sh` |

**CLI smoke test:**

```bash
curl -s http://127.0.0.1:8081/health
curl -s -X POST http://127.0.0.1:8081/v1/ask \
  -H 'Content-Type: application/json' \
  -d '{"text":"what can you do?"}'
```

---

## Development

```bash
git clone https://github.com/prititaliya/macAgent.git
cd MacAgent
./automation/setup_backend.sh
./automation/open_macagent.sh    # Debug build + launch
./automation/make_dmg.sh         # Release DMG → dist/MacAgent-1.0.1.dmg
```

### Release (maintainers)

Version is defined in [`VERSION`](VERSION). Pushing a tag triggers CI to build the DMG and publish a GitHub Release:

```bash
./automation/sync_version.sh   # sync VERSION → Info.plist, main.py
git add VERSION && git commit -m "Bump version to 1.0.1"
git tag v1.0.1
git push origin main --tags
```

---

## Project layout

| Path | Role |
|---|---|
| `MacAgentApp/` | SwiftUI overlay (primary UI) |
| `main.py` | FastAPI daemon |
| `tools/agent_loop.py` | Multi-step agent + goal checks |
| `llm/inference.py` | Local GGUF planner / answers |
| `automation/` | setup, DMG build, dev scripts |
| `config/settings.json` | Model path, TTS, port |

The older `MacAgent/` Dock UI is **deprecated** — use `MacAgentApp/`.

---

## One-liner

> MacAgent is a **local tool-using agent for your Mac**: summon it over any app, talk or type, and it completes multi-step tasks (files, apps, web, UI) with an Approve gate for anything destructive — without sending your desktop to a cloud assistant.
