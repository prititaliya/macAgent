# MacAgent

**A local macOS overlay agent** — press a shortcut, ask in plain English (type or speak), and it *does things on your Mac*: opens apps, finds and deletes files, searches the web with retries, controls the UI, and asks you to Approve before anything destructive.

Runs entirely on your machine (small local LLM + Python tools). Not a cloud chatbot pasted into a window.

---

## What it can do

| Capability | Example asks |
|---|---|
| **Answer + act in a loop** | Multi-step goals: find → act → confirm. Won’t stop at “here’s the filename” when you asked to delete it. |
| **Files & Downloads** | “What’s my latest download?” · “Delete the file I downloaded last” *(Approve first)* · “Open my Downloads folder” |
| **Apps & Chrome** | “Open Slack” · “Open https://github.com in Chrome” |
| **Web research** | “Compare frontier LLM models and their prices” — searches, scrapes pages, **retries with a sharper query** if the first answer lacks facts |
| **System power / Trash** | “Empty the bin” · “Shut down my Mac” · “Put my computer to sleep” — always **Approve / Deny** first |
| **System Settings** | “Open Wi‑Fi settings” · “Open Accessibility settings” |
| **Screen control** | Click, type, menus via Accessibility *(MacAgent.app must be enabled once)* |
| **Shell & Python** | Short local bash / Python for compute and file tasks |
| **Personal memory** | Notes in Preferences (“what do you know about me?”) |
| **Voice** | Mic in the overlay (macOS speech) **or** FreeFlow → same agent |

### Safety

Destructive actions (`rm`, empty Trash, shut down, restart, sleep) never run silently. The overlay shows an **Approve / Deny** card with the exact command.

Casual chat (“yo”, “hey”) does **not** invent shutdowns or deletes.

---

## Demo script (show someone in ~2 minutes)

1. Launch MacAgent → **⌃⌥Space** to show the overlay.
2. **Ask:** `what's my latest download?` → names the newest file in Downloads.
3. **Ask:** `delete the file I downloaded latest` → Approve card → Approve → file gone.
4. **Ask:** `compare frontier LLM models and their prices` → watch activity logs: search → maybe “searching again…” → answer with sources.
5. **Ask:** `open Slack` → app launches.
6. **Ask:** `yo` → friendly reply, no tools.

Optional: tap the **mic** and dictate instead of typing.

---

## How it works (one picture)

```text
  You (⌃⌥Space overlay / mic / FreeFlow)
              │
              ▼
     Local FastAPI daemon (:8081)
              │
              ▼
   Multi-step agent loop (local Qwen)
      ├─ web_search  (DuckDuckGo + page scrape, retries if thin)
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

## Quick start

**Requirements:** macOS 13+ Apple Silicon, Xcode (or XcodeGen), Python 3.10+, project `venv`, Qwen2.5-1.5B-Instruct Q4_K_M GGUF (default `~/Models/qwen2.5-1.5b-instruct-q4_k_m.gguf`). Chrome recommended for URL opens.

```bash
cd /path/to/MacAgent
# TTS (Kokoro) system deps
brew install espeak-ng portaudio libsndfile

python3 -m venv venv && source venv/bin/activate
CMAKE_ARGS="-DGGML_METAL=on -DCMAKE_OSX_ARCHITECTURES=arm64" ARCHFLAGS="-arch arm64" \
  pip install -r requirements.txt

./automation/open_macagent.sh   # build + launch overlay
```

Spoken status + final answers use [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (downloaded on first use into the Hugging Face cache). Toggle volume / TTS in Preferences → Settings.

- **⌃⌥Space** — show/hide overlay (grant **Accessibility** when prompted; enable **MacAgent.app**, not AEServer)
- First mic use: allow **Microphone** + **Speech Recognition**
- Menu bar sparkles → Preferences (sites, apps, notes, history)

### FreeFlow (optional voice)

1. STT as usual (e.g. Groq).
2. Post-processing / LLM base URL: `http://127.0.0.1:8081/v1`
3. Keep MacAgent running while you dictate.

```bash
defaults write com.zachlatta.freeflow post_processing_timeout_seconds -float 120
```

---

## Permissions checklist

| Permission | Why |
|---|---|
| Accessibility | Global hotkey + UI click/type |
| Microphone + Speech Recognition | In-overlay dictation |
| Full Disk / Spotlight (helpful) | Better file find via `mdfind` |

---

## CLI smoke test

```bash
curl -s http://127.0.0.1:8081/health
curl -s -X POST http://127.0.0.1:8081/v1/ask \
  -H 'Content-Type: application/json' \
  -d '{"text":"what can you do?"}'
curl -N http://127.0.0.1:8081/v1/events
```

Logs: `~/Library/Logs/MacAgent/`.

---

## Project layout

| Path | Role |
|---|---|
| `MacAgentApp/` | SwiftUI overlay (primary UI) |
| `main.py` | FastAPI daemon |
| `tools/agent_loop.py` | Multi-step agent + goal checks + search retries |
| `llm/inference.py` | Local GGUF planner / answers |
| `automation/` | open/kill scripts, optional Chrome host |

The older `MacAgent/` Dock UI is **deprecated** — use `MacAgentApp/`.

---

## One-liner pitch

> MacAgent is a **local tool-using agent for your Mac**: summon it over any app, talk or type, and it completes multi-step tasks (files, apps, web, UI) with an Approve gate for anything destructive — without sending your desktop to a cloud assistant.
