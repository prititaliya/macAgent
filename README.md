# MacAgent

One native **SwiftUI desktop app** for voice-driven macOS help on 8GB Apple Silicon.

- **Live** — you speak (FreeFlow Fn) → local Qwen classifies → answer in the app, or open an app/site
- **History** — activity log (you said → result)
- **Sites** — purpose-tagged URLs (“watch football” → your ESPN URL)
- **Status** — daemon + model health

Opening the app starts the local FastAPI daemon (`:8081`) if it is not already running. Quitting stops a daemon the app started.

## Requirements

- macOS 13+ Apple Silicon
- Python 3.10+
- Xcode Command Line Tools (`swiftc`)
- [FreeFlow](https://github.com/zachlatta/freeflow) for speech-to-text
- Qwen2.5-1.5B-Instruct Q4_K_M GGUF (default `~/Models/qwen2.5-1.5b-instruct-q4_k_m.gguf`)
- Google Chrome (for site opens + optional login fill)

## Boot (once)

```bash
cd /Users/jatin/Projects/MacAgent
python3 -m venv venv && source venv/bin/activate
CMAKE_ARGS="-DGGML_METAL=on -DCMAKE_OSX_ARCHITECTURES=arm64" ARCHFLAGS="-arch arm64" \
  pip install -r requirements.txt
chmod +x automation/*.sh MacAgent/build.sh
./MacAgent/build.sh
```

## Open the app

```bash
./automation/open_macagent.sh
```

Or open `MacAgent/build/MacAgent.app` from Finder.

**Hotkey (optional):** Shortcuts → Run Shell Script → `/Users/jatin/Projects/MacAgent/automation/open_macagent.sh`

## FreeFlow wiring

1. Transcription: Groq (or other STT) as usual.
2. Post-processing / LLM API base: `http://127.0.0.1:8081/v1`
3. Keep MacAgent open while you dictate (daemon stays up with the app).

```bash
defaults write com.zachlatta.freeflow post_processing_timeout_seconds -float 120
```

## How it works

1. FreeFlow sends the transcript to the local daemon.
2. Intent: `answer` | `browse` | `open_app` | `open_site` | `search_fallback` (local model + heuristics).
3. Questions → short local answer → Live panel + History.
4. Questions / live info → DuckDuckGo search → **read page text** → local grounded answer in Live, with **Sources** links (Chrome is not opened automatically).
5. Orders (`open …`) → aliases / purpose sites / history → Live + History.

Not a ChatGPT clone — each utterance is answer-or-act for your Mac.

DuckDuckGo access is keyless and best-effort (unofficial library; may rate-limit). There is no official unlimited DDG API key product; this uses their public search HTML/API endpoints through `ddgs`.

## Optional: LaunchAgent

If you want the daemon without the app UI, `./automation/install_launch_agent.sh install` still works. Prefer the app-managed daemon for day-to-day use.

## Chrome login autofill (optional)

1. `./automation/install_native_host.sh`
2. Chrome → Load unpacked → `automation/extension`
3. `./automation/install_native_host.sh --extension-id=<ID>`

## CLI

```bash
curl -s http://127.0.0.1:8081/health
curl -s http://127.0.0.1:8081/v1/activity?limit=20
curl -N http://127.0.0.1:8081/v1/events
```

Logs: `~/Library/Logs/MacAgent/`.
