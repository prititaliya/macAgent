# MacAgent

Transparent **overlay agent** for macOS (8GB Apple Silicon): summon it over whatever you are doing, type or speak, and it uses **tools** (files, notes, apps, sites, web, System Settings panes).

## Primary UI (Xcode)

Open and run the Xcode app:

```bash
open /Users/jatin/Projects/MacAgent/MacAgentApp/MacAgent.xcodeproj
```

Or from the terminal:

```bash
./automation/open_macagent.sh
```

- **⌃⌥Space** — show/hide the floating overlay (grant **Accessibility** when prompted)
- Menu bar icon — Show Agent / Preferences / Quit
- **Preferences** — Sites, Apps, Settings notes, Debug, History (secondary; not the main experience)
- Overlay talks to the Python daemon via `POST /v1/ask` + SSE `/v1/events`
- Voice still uses **FreeFlow** → `http://127.0.0.1:8081/v1`

App Sandbox is **off** so the agent can spawn the daemon, search files (Spotlight), and open apps/URLs.

## Requirements

- macOS 13+ Apple Silicon
- Xcode (or XcodeGen + `xcodebuild`)
- Python 3.10+ + project `venv`
- [FreeFlow](https://github.com/zachlatta/freeflow) for speech-to-text
- Qwen2.5-1.5B-Instruct Q4_K_M GGUF (default `~/Models/qwen2.5-1.5b-instruct-q4_k_m.gguf`)
- Google Chrome (for site opens)
- **Full Disk / Spotlight** access helps `find_files` (mdfind)

## Boot (once)

```bash
cd /Users/jatin/Projects/MacAgent
python3 -m venv venv && source venv/bin/activate
CMAKE_ARGS="-DGGML_METAL=on -DCMAKE_OSX_ARCHITECTURES=arm64" ARCHFLAGS="-arch arm64" \
  pip install -r requirements.txt
```

Regenerate the Xcode project after `project.yml` changes (optional):

```bash
cd MacAgentApp && xcodegen generate
```

## FreeFlow wiring

1. Transcription: Groq (or other STT) as usual.
2. Post-processing / LLM API base: `http://127.0.0.1:8081/v1`
3. Keep MacAgent running while you dictate (daemon stays up with the app).

```bash
defaults write com.zachlatta.freeflow post_processing_timeout_seconds -float 120
```

## How it works

1. Overlay or FreeFlow sends text to the local FastAPI daemon (`:8081`).
2. **Agent tool loop** (capped ~4 steps) — model emits structured tool calls:
   - `find_files` — Spotlight / limited home search
   - `get_user_context` / `update_user_context` — Settings notes
   - `list_apps` / `open_app` / `list_sites` / `open_url`
   - `web_search` — DuckDuckGo grounded Q&A (**no auto-open** unless `open_url`)
   - `run_python` — write & run short Python (math, scripts; blocked network/system APIs)
   - `open_system_settings` — whitelisted panes only
   - `respond` — final answer shown in the overlay
3. Debug traces log each tool call under Preferences → Debug.

Not a ChatGPT clone — each utterance is tool-use-or-answer for your Mac.

DuckDuckGo access is keyless and best-effort (`ddgs`).

## Deprecated: `MacAgent/build.sh`

The older `swiftc` / `MacAgent/build.sh` Dock multi-tab app is **deprecated**. Prefer `MacAgentApp/` (Xcode). The `MacAgent/` folder remains as reference only.

## Optional: LaunchAgent

If you want the daemon without the app UI, `./automation/install_launch_agent.sh install` still works. Prefer the app-managed daemon for day-to-day use.

## Chrome login autofill (optional)

1. `./automation/install_native_host.sh`
2. Chrome → Load unpacked → `automation/extension`
3. `./automation/install_native_host.sh --extension-id=<ID>`

## CLI

```bash
curl -s http://127.0.0.1:8081/health
curl -s -X POST http://127.0.0.1:8081/v1/ask -H 'Content-Type: application/json' -d '{"text":"open wifi settings"}'
curl -s http://127.0.0.1:8081/v1/activity?limit=20
curl -N http://127.0.0.1:8081/v1/events
```

Logs: `~/Library/Logs/MacAgent/`.
