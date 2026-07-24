# Changelog

All notable changes to MacAgent are documented here.

## [1.5.0] — 2026-07-23

### Highlights

- **Smarter hybrid asks** — “search the web for the latest X, check what’s on my Mac, am I out of date?” now runs web research **and** a local shell check, then compares versions deterministically (no “run `python3 --version` yourself” dead-ends).
- **Progressive web scraping** — search scrapes **one** page first; only fetches the next URL if the answer is still thin (up to 3 pages), instead of pulling every hit up front.
- **Cloud for scraped answers** — when page content is scraped and cloud is configured, MacAgent answers with the cloud model (local GGUF still plans Mac actions).
- **More reliable Mac actions** — better goal completion for GUI (Calculator type/read), empty discovery results, bash `&&` soft-failures with useful stdout, and Accessibility trust hints after adhoc rebuilds.
- **Local answer quality** — higher token budgets for homework/knowledge, prompt compaction after long tool chains, slimmer planner catalog to reduce UI crashes from huge debug dumps.

### Web & research

- DuckDuckGo search + one-at-a-time page extract (`tools/duckduckgo.py`)
- Retry path: next unread URL → then a sharper search query
- Hybrid version compare without trusting tiny-model arithmetic

### Agent loop

- Forced follow-ups when the model stops at advice or open-app-only
- Coerce messy version chains (`python3 && python && pip`) → `python3 --version`
- Soft-ok when stdout is useful but a later `&&` link exits non-zero

### App / packaging

- Version **1.5.0** (build 15) in `VERSION`, Info.plist, `project.yml`, FastAPI

---

## [1.0.1] — prior

Initial public DMG packaging, overlay + FastAPI daemon, Approve gate for destructive actions, local Qwen3-4B planning, optional cloud routing, voice (mic + Kokoro TTS).
