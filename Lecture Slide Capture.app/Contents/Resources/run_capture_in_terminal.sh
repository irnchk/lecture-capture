#!/bin/bash
set -euo pipefail

MODE="${1:-capture}"
WINDOW_ID_RAW=""
OUTPUT_BASE_RAW="${2:-}"
WINDOW_ID=""

APP_CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
RES_DIR="$APP_CONTENTS/Resources"
SCRIPT_PATH="$RES_DIR/slide_capture.py"
REQ_PATH="$RES_DIR/requirements.txt"
CONFIG_DIR="$HOME/Library/Application Support/LectureSlideCapture"
OUTPUT_BASE_FILE="$CONFIG_DIR/output_base.txt"
DEFAULT_OUTPUT_BASE="$HOME/Documents/Lecture Slide Capture"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

normalize_path() {
  local value="${1:-}"
  value="${value//$'\r'/}"
  if [[ -z "$value" ]]; then
    value="$DEFAULT_OUTPUT_BASE"
  fi
  if [[ "$value" != "/" ]]; then
    value="${value%/}"
  fi
  if [[ -z "$value" ]]; then
    value="$DEFAULT_OUTPUT_BASE"
  fi
  printf '%s' "$value"
}

load_output_base() {
  local value="${OUTPUT_BASE_RAW:-}"
  if [[ -z "$value" && -f "$OUTPUT_BASE_FILE" ]]; then
    value="$(cat "$OUTPUT_BASE_FILE" 2>/dev/null || true)"
  fi
  normalize_path "$value"
}

OUTPUT_BASE="$(load_output_base)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
OUTPUT_DIR="$OUTPUT_BASE/$TIMESTAMP"

pause_and_exit() {
  local status="${1:-0}"
  echo
  read -r -p "Press Enter to close this window..." _ || true
  exit "$status"
}

print_header() {
  echo "=============================================="
  echo " Lecture Slide Capture"
  echo "=============================================="
  echo "Mode: $MODE"
  echo "Base output folder: $OUTPUT_BASE"
  echo "Current session folder: $OUTPUT_DIR"
  if [[ -n "$WINDOW_ID" ]]; then
    echo "Target window ID: $WINDOW_ID"
  elif [[ "$MODE" == "capture" ]]; then
    echo "Target window: choose from the current list"
  else
    echo "Target window ID: auto-select"
  fi
  echo
}

if [[ -z "$PYTHON_BIN" ]]; then
  echo "[error] Could not find python3."
  echo "Install Python 3 and run again."
  pause_and_exit 1
fi

missing_modules="$($PYTHON_BIN - <<'PY' 2>/dev/null || true
import importlib.util
mods = ["cv2", "mss", "skimage", "PIL", "img2pdf", "numpy", "Quartz", "Cocoa"]
missing = [m for m in mods if importlib.util.find_spec(m) is None]
print(" ".join(missing))
PY
)"

if [[ -n "$missing_modules" ]]; then
  echo "[info] Some required Python packages are missing: $missing_modules"
  read -r -p "Install them now? [Y/n] " INSTALL_REPLY || INSTALL_REPLY="Y"
  INSTALL_REPLY="${INSTALL_REPLY:-Y}"
  case "$INSTALL_REPLY" in
    [Nn]*)
      echo
      echo "Install with this command, then run again:"
      echo "  python3 -m pip install --user -r \"$REQ_PATH\""
      pause_and_exit 1
      ;;
    *)
      echo
      echo "Installing packages..."
      "$PYTHON_BIN" -m pip install --user -r "$REQ_PATH"
      ;;
  esac
fi

mkdir -p "$OUTPUT_DIR"
print_header

cmd=("$PYTHON_BIN" "$SCRIPT_PATH"
  --output "$OUTPUT_DIR"
  --capture-source window
  --window-owner "Google Chrome"
  --window-backend auto
  --preview
  --mode slide
  --make-pdf
)

if [[ "$MODE" == "list" ]]; then
  cmd=("$PYTHON_BIN" "$SCRIPT_PATH"
    --capture-source window
    --window-owner "Google Chrome"
    --list-windows
  )
else
  cmd+=(--choose-window)
fi

printf '[run] '
printf '%q ' "${cmd[@]}"
printf '\n\n'

set +e
"${cmd[@]}"
STATUS=$?
set -e

echo
if [[ "$STATUS" -eq 0 ]]; then
  if [[ "$MODE" == "capture" ]]; then
    echo "[done] Output: $OUTPUT_DIR"
  else
    echo "[done] Finished listing windows."
  fi
else
  echo "[exit] Program ended with status code $STATUS."
fi

pause_and_exit "$STATUS"
