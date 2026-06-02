#!/bin/bash
set -u

APP_CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
RES_DIR="$APP_CONTENTS/Resources"
SCRIPT_PATH="$RES_DIR/slide_capture.py"
REQ_PATH="$RES_DIR/requirements.txt"
CONFIG_DIR="$HOME/Library/Application Support/LectureSlideCapture"
OUTPUT_BASE_FILE="$CONFIG_DIR/output_base.txt"
DEFAULT_OUTPUT_BASE="$HOME/Documents/Lecture Slide Capture"
LOG_DIR="$HOME/Library/Logs/LectureSlideCapture"
LOG_PATH="$LOG_DIR/terminal_session.log"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

mkdir -p "$CONFIG_DIR" "$LOG_DIR"
if command -v tee >/dev/null 2>&1; then
  exec > >(tee -a "$LOG_PATH") 2>&1
else
  exec >> "$LOG_PATH" 2>&1
fi

echo "=============================================="
echo " Lecture Slide Capture launcher"
echo "=============================================="
date '+Started: %Y-%m-%d %H:%M:%S'
echo

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
  local value=""
  if [[ -f "$OUTPUT_BASE_FILE" ]]; then
    value="$(cat "$OUTPUT_BASE_FILE" 2>/dev/null || true)"
  fi
  normalize_path "$value"
}

save_output_base() {
  local value
  value="$(normalize_path "$1")"
  mkdir -p "$CONFIG_DIR"
  printf '%s\n' "$value" > "$OUTPUT_BASE_FILE"
}

as_applescript_string() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//"/\\"}
  printf '"%s"' "$value"
}

run_osascript() {
  /usr/bin/osascript <<APPLESCRIPT
$1
APPLESCRIPT
}

prompt_menu() {
  local current="$1"
  local message="Current base output folder:\n$current\n\nA timestamped session folder will be created under this path when capture starts."
  local result
  result=$(run_osascript "try
  set picked to button returned of (display dialog $(as_applescript_string "$message") with title \"Lecture Slide Capture\" buttons {\"Cancel\", \"Open in Finder\", \"Choose Folder\", \"List Windows\", \"Start Capture\"} default button \"Start Capture\" cancel button \"Cancel\")
  return picked
on error number -128
  return \"__CANCEL__\"
end try" 2>/dev/null) || result=""
  if [[ -z "$result" ]]; then
    echo >&2
    echo "Current base output folder: $current" >&2
    echo "Menu:" >&2
    echo "  1) Start Capture" >&2
    echo "  2) List Windows" >&2
    echo "  3) Choose Folder" >&2
    echo "  4) Open in Finder" >&2
    echo "  5) Cancel" >&2
    echo >&2
    read -r -p "Input [1=Start Capture, 2=List Windows, 3=Choose Folder, 4=Open in Finder, 5=Cancel]: " text_choice || text_choice="5"
    case "$text_choice" in
      1) result="Start Capture" ;;
      2) result="List Windows" ;;
      3) result="Choose Folder" ;;
      4) result="Open in Finder" ;;
      *) result="__CANCEL__" ;;
    esac
  fi
  printf '%s' "$result"
}

choose_output_base() {
  local current="$1"
  if [[ ! -d "$current" ]]; then
    current="$HOME"
  fi
  local picked
  picked=$(run_osascript "try
  set defaultLocation to POSIX file $(as_applescript_string "$current")
  set pickedFolder to choose folder with prompt \"Choose the base folder for captured slides.\" default location defaultLocation
  return POSIX path of pickedFolder
on error number -128
  return \"__CANCEL__\"
end try" 2>/dev/null) || picked=""

  if [[ -z "$picked" ]]; then
    echo
    read -r -e -p "Enter the new base output folder: " picked || picked=""
  fi

  if [[ -n "$picked" && "$picked" != "__CANCEL__" ]]; then
    save_output_base "$picked"
    echo "[settings] Base output folder changed: $(load_output_base)"
  fi
}

open_output_base_in_finder() {
  local target="$1"
  target="$(normalize_path "$target")"
  mkdir -p "$target"
  /usr/bin/open "$target"
}

pause_and_exit() {
  local status="${1:-0}"
  echo
  read -r -p "Press Enter to close this window..." _ || true
  exit "$status"
}

ensure_python() {
  if [[ -z "$PYTHON_BIN" ]]; then
    echo "[error] Could not find python3."
    pause_and_exit 1
  fi
}

ensure_requirements() {
  local missing_modules
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
        "$PYTHON_BIN" -m pip install --user -r "$REQ_PATH" || pause_and_exit 1
        ;;
    esac
  fi
}

show_window_list() {
  echo >&2
  echo "[Current Chrome windows]" >&2
  "$PYTHON_BIN" "$SCRIPT_PATH" --capture-source window --window-owner "Google Chrome" --list-windows >&2
  local status=$?
  if [[ "$status" -ne 0 ]]; then
    echo >&2
    echo "[info] No match for Google Chrome, retrying with Chrome." >&2
    "$PYTHON_BIN" "$SCRIPT_PATH" --capture-source window --window-owner "Chrome" --list-windows >&2 || true
  fi
  echo >&2
}

choose_window_id_text() {
  local reply=""
  while true; do
    show_window_list
    read -r -p "Enter a window number or window ID. Enter=auto-select, r=refresh, q=cancel: " reply || reply="q"
    case "$reply" in
      "")
        printf '%s' ""
        return 0
        ;;
      [Qq]*)
        printf '%s' "__CANCEL__"
        return 0
        ;;
      [Rr]*)
        continue
        ;;
      *)
        if [[ "$reply" =~ ^[0-9]+$ ]]; then
          printf '%s' "$reply"
          return 0
        fi
        echo "[info] Enter a numeric candidate number, a real window ID, or press Enter." >&2
        ;;
    esac
  done
}

ensure_python
ensure_requirements

while true; do
  CURRENT_OUTPUT_BASE="$(load_output_base)"
  MENU_CHOICE="$(prompt_menu "$CURRENT_OUTPUT_BASE")"
  case "$MENU_CHOICE" in
    "__CANCEL__"|"Cancel")
      exit 0
      ;;
    "Open in Finder")
      open_output_base_in_finder "$CURRENT_OUTPUT_BASE"
      ;;
    "Choose Folder")
      choose_output_base "$CURRENT_OUTPUT_BASE"
      ;;
    "List Windows")
      show_window_list
      read -r -p "Press Enter to return to the menu..." _ || true
      ;;
    *)
      break
      ;;
  esac
done

CURRENT_OUTPUT_BASE="$(load_output_base)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
OUTPUT_DIR="$CURRENT_OUTPUT_BASE/$TIMESTAMP"
mkdir -p "$OUTPUT_DIR"
WINDOW_ID="$(choose_window_id_text)"
if [[ "$WINDOW_ID" == "__CANCEL__" ]]; then
  pause_and_exit 0
fi

echo
printf '[output] %s\n' "$OUTPUT_DIR"

declare -a cmd
cmd=("$PYTHON_BIN" "$SCRIPT_PATH"
  --output "$OUTPUT_DIR"
  --capture-source window
  --window-owner "Google Chrome"
  --window-backend auto
  --preview
  --mode slide
  --make-pdf
)

if [[ -n "$WINDOW_ID" ]]; then
  cmd+=(--window-id "$WINDOW_ID")
fi

echo
printf '[run] '
printf '%q ' "${cmd[@]}"
printf '\n\n'

set +e
"${cmd[@]}"
STATUS=$?
set -e

echo
if [[ "$STATUS" -eq 0 ]]; then
  echo "[done] Output: $OUTPUT_DIR"
else
  echo "[exit] Program ended with status code $STATUS."
fi

pause_and_exit "$STATUS"
