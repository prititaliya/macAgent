# MacAgent

[![Release](https://img.shields.io/github/v/release/prititaliya/macAgent?label=download)](https://github.com/prititaliya/macAgent/releases/latest)
[![CI](https://github.com/prititaliya/macAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/prititaliya/macAgent/actions/workflows/ci.yml)

**A local macOS overlay agent** — press a shortcut, ask in plain English (type or speak), and it *does things on your Mac*: opens apps, finds and deletes files, searches the web, controls the UI, and asks you to **Approve** before anything destructive.

Runs on your machine (local LLM + Python tools), with optional cloud for hard knowledge / scraped web answers. Not a cloud chatbot pasted into a window.

**Current release: [v1.5.0](https://github.com/prititaliya/macAgent/releases/tag/v1.5.0)** — see [CHANGELOG](CHANGELOG.md).

---

## Download

| | |
|---|---|
| **macOS app** | [**Download MacAgent.dmg**](https://github.com/prititaliya/macAgent/releases/latest/download/MacAgent.dmg) |
| **Requirements** | macOS 13+, Apple Silicon (M1/M2/M3/M4) |
| **Latest releases** | [github.com/prititaliya/macAgent/releases](https://github.com/prititaliya/macAgent/releases) |

The DMG contains the menu-bar overlay app. You also need the Python backend (one-time setup below) — the app auto-starts it when you summon the overlay.

---

## What's new in 1.5

- **Hybrid research** — e.g. latest Python online + `python3 --version` on this Mac + clear out-of-date verdict
- **One-page-at-a-time scraping** — fetch another source only if the first isn’t enough
- **Cloud for scraped pages** — when cloud is enabled in Preferences, page-grounded answers use the cloud model
- **Sturdier agent loop** — fewer fake “Done” / shell-advice finishes; better Calculator / Accessibility flows

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

**Supported models (≤~5.5 GB file size)** — drop an Instruct GGUF into `~/Models`, then pick it in Preferences / the overlay Model chip. Switching reloads the weights and applies the correct chat template:

| Model | Typical Q4 size | Notes |
|---|---|---|
| [Qwen3-4B](https://huggingface.co/Qwen/Qwen3-4B-GGUF) | ~2.3 GB | Default / recommended |
| [Llama 3.2 3B Instruct](https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF) | ~2 GB | General chat |
| [Phi-3.5 Mini Instruct](https://huggingface.co/bartowski/Phi-3.5-mini-instruct-GGUF) | ~2.5 GB | Logic / everyday tasks |
| [Qwen 2.5 7B Instruct Q4_K_M](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF) | ~4.4 GB | Stronger coding/math (tight on 8GB) |
| [SmolLM2 1.7B](https://huggingface.co/HuggingFaceTB/SmolLM2-1.7B-Instruct-GGUF) | ~1 GB | Ultra-light backup |

Prefer **Q4_K_M** (or Q5) quants. On 8GB Macs, keep context modest; MacAgent already caps `n_ctx` under memory pressure. GGUFs ≥~5.5 GB are rejected. Qwen3-30B-A3B needs ~10GB+ free disk and typically 16GB+ unified memory.

**Optional cloud** — Preferences → Cloud: enable a provider (e.g. DeepSeek / OpenAI-compatible) for knowledge and scraped-web answers. Mac file/UI actions still plan locally.

### 3. Grant permissions

| Permission | Why | When |
|---|---|---|
| **Accessibility** | Global hotkey + UI click/type | First launch — enable **MacAgent.app**, not AEServer. After each adhoc rebuild, toggle Off→On and relaunch the app from `/Applications`. |
| **Microphone** | In-overlay dictation | First mic tap |
| **Speech Recognition** | On-device speech-to-text | First mic tap |
| **Full Disk Access** *(optional)* | Better file search via Spotlight | System Settings → Privacy |

### 4. Use it

- **⌃⌥Space** (Control-Option-Space) — show/hide the floating overlay (top-right corner)
- Type a question or tap the **mic** to dictate
- Menu bar icon → **Preferences** (sites, apps, notes, history, auto-hide, cloud)
- Overlay chips: **Model** · **Search** (Auto / On / Off) · **Follow-up**
- Toggle voice output in Preferences → Settings

Spoken status + answers use [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (downloaded on first use).

---

## What it can do

| Capability | Example asks |
|---|---|
| **Answer + act in a loop** | Multi-step goals: find → act → confirm |
| **Files & Downloads** | “What's my latest download?” · “Delete the file I downloaded last” *(Approve first)* |
| **Apps & Chrome** | “Open Slack” · “Open https://github.com in Chrome” |
| **Web research** | “Compare frontier LLM models and their prices” — search, scrape one page, fetch more only if needed |
| **Hybrid: web + this Mac** | “Latest stable Python online, what’s installed here, am I out of date?” |
| **System power / Trash** | “Empty the bin” · “Shut down my Mac” — always **Approve / Deny** first |
| **Screen control** | Open Calculator, type an expression, read the result via Accessibility |
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
4. **Ask:** `compare frontier LLM models and their prices` → activity: search → (maybe next page) → answer.
5. **Ask:** `Search the web for the latest stable Python, check what's installed on my Mac, am I out of date?` → latest + local version + verdict.
6. **Ask:** `open Slack` → app launches.
7. **Ask:** `yo` → friendly reply, no tools.

---

## Architecture

```text
  You (⌃⌥Space overlay / mic)          STT: on-device Speech · TTS: Kokoro
              │
              ▼
     Local FastAPI daemon (:8081)
        POST /v1/ask  or  /v1/chat
              │
              ▼
   Agent loop — ChatML context + tool schema
              │
              ▼
   Local GGUF (Qwen3-4B)  +  optional cloud (scraped web / knowledge)
      n_ctx up to 32K (RAM-capped) · temp 0.2 · min_p 0.05
              │
        ┌─────┴─────┐
   tool call     final respond
        │
        ▼
   Destructive? ──yes──▶ Approve/Deny overlay ──▶ execute → append tool turn → loop
        │ no
        ▼
   Execute (bash / python / UI / web) → append tool result → back to planner
```

Web search uses DuckDuckGo + progressive page extract (one URL at a time). Inference knobs live in `config/settings.json` (`n_ctx`, `temperature`, `min_p`, `flash_attn`, `cloud`).

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
| Overlay shows “Starting daemon…” forever | Run `./automation/setup_backend.sh` and confirm `~/MacAgent` (or your clone) `venv` exists |
| Daemon won't start | Check `~/Library/Logs/MacAgent/daemon.err` |
| Model errors | Verify `model_path` in `config/settings.json` points to a valid `.gguf` file |
| Hotkey doesn't work | System Settings → Privacy → Accessibility → enable **MacAgent** |
| UI click/type fails after rebuild | Accessibility: MacAgent Off→On, quit menu-bar app, reopen `/Applications/MacAgent.app` |
| DMG says app is “damaged” | Gatekeeper quarantine on unsigned build. After dragging to Applications: `xattr -cr /Applications/MacAgent.app` then open again |
| Cloud answers still say local model | Enable cloud + API key in Preferences; scraped pages force cloud when configured |
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
SKIP_INSTALL=1 ./automation/make_dmg.sh   # Release DMG → dist/MacAgent-1.5.0.dmg
```

### Release (maintainers)

Version is defined in [`VERSION`](VERSION). Pushing a tag triggers CI to build the DMG and publish a GitHub Release:

```bash
echo 1.5.0 > VERSION
./automation/sync_version.sh
git add VERSION CHANGELOG.md README.md MacAgentApp/Sources/Info.plist MacAgentApp/project.yml main.py
git commit -m "Release 1.5.0"
git tag v1.5.0
git push origin main --tags
```

---

## Project layout

| Path | Role |
|---|---|
| `MacAgentApp/` | SwiftUI overlay (primary UI) |
| `main.py` | FastAPI daemon |
| `tools/agent_loop.py` | Multi-step agent + goal checks |
| `tools/duckduckgo.py` | Search + progressive page scrape |
| `llm/inference.py` | Local GGUF planner / answers |
| `llm/cloud.py` | Optional OpenAI-compatible cloud |
| `automation/` | setup, DMG build, dev scripts |
| `config/settings.example.json` | Template settings (copy → `settings.json`, gitignored) |
| `config/settings.json` | Local model path, TTS, port, cloud API key *(not in git)* |
| `CHANGELOG.md` | Release notes |

The older `MacAgent/` Dock UI is **deprecated** — use `MacAgentApp/`.

---

## One-liner

> MacAgent is a **local tool-using agent for your Mac**: summon it over any app, talk or type, and it completes multi-step tasks (files, apps, web, UI) with an Approve gate for anything destructive — without sending your desktop to a cloud assistant.
