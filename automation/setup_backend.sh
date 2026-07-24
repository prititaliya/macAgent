#!/usr/bin/env bash
# One-time Python backend setup for MacAgent (run from repo root).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> MacAgent backend setup"
echo "    Repo: $ROOT"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh" >&2
  exit 1
fi

echo "==> Installing system dependencies (TTS + audio)…"
brew install espeak-ng portaudio libsndfile python@3.12 2>/dev/null || brew install espeak-ng portaudio libsndfile

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON="$(brew --prefix python@3.12 2>/dev/null)/bin/python3" || true
fi

echo "==> Creating virtualenv…"
"$PYTHON" -m venv venv
source venv/bin/activate

echo "==> Installing Python packages (Metal llama-cpp)…"
CMAKE_ARGS="-DGGML_METAL=on -DCMAKE_OSX_ARCHITECTURES=arm64" \
ARCHFLAGS="-arch arm64" \
  pip install --upgrade pip
CMAKE_ARGS="-DGGML_METAL=on -DCMAKE_OSX_ARCHITECTURES=arm64" \
ARCHFLAGS="-arch arm64" \
  pip install -r requirements.txt

MODEL_DIR="${HOME}/Models"
MODEL_FILE="${MODEL_DIR}/Qwen3-4B-Q4_K_M.gguf"
MODEL_URL="https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf"

mkdir -p "$MODEL_DIR"
if [[ ! -f "$MODEL_FILE" ]]; then
  echo "==> Downloading Qwen3-4B Q4_K_M (~2.3 GB) to ${MODEL_FILE}…"
  curl -L --progress-bar -o "$MODEL_FILE" "$MODEL_URL"
else
  echo "==> Model already present: ${MODEL_FILE}"
fi

SETTINGS="$ROOT/config/settings.json"
if [[ -f "$SETTINGS" ]]; then
  python3 - <<PY
import json, os
path = "$SETTINGS"
model = os.path.expanduser("$MODEL_FILE")
model_dir = os.path.expanduser("$MODEL_DIR")
with open(path) as f:
    s = json.load(f)
s["model_path"] = model
s["model_dir"] = model_dir
with open(path, "w") as f:
    json.dump(s, f, indent=2)
    f.write("\n")
print(f"==> Set config/settings.json model_path → {model}")
PY
fi

cat <<EOF

✓ Backend ready.

Default model: Qwen3-4B Q4_K_M in ${MODEL_DIR}

Optional light GGUFs (drop into ${MODEL_DIR}, then pick in Preferences):
  • Llama 3.2 3B Instruct Q4_K_M  — general chat (~2 GB)
  • Phi-3.5 Mini Instruct Q4_K_M  — everyday logic (~2.5 GB)
  • Qwen 2.5 7B Instruct Q4_K_M   — coding/math (~4.4 GB; tight on 8GB RAM)
  • SmolLM2 1.7B Instruct          — ultra-light backup (~1 GB)

Next:
  1. Install MacAgent.app from the release DMG (if you have not already).
  2. Open MacAgent → grant Accessibility + Microphone when prompted.
  3. Press Control-Option-Space to summon the overlay.

Smoke test:
  source venv/bin/activate && python main.py &
  curl -s http://127.0.0.1:8081/health

Logs: ~/Library/Logs/MacAgent/
EOF
