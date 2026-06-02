#!/bin/bash
set -u

APP_CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
RES_DIR="$APP_CONTENTS/Resources"
SCRIPT_PATH="$RES_DIR/slide_capture.py"
REQ_PATH="$RES_DIR/requirements.txt"
CONFIG_DIR="$HOME/Library/Application Support/LectureSlideCapture"
OUTPUT_BASE_FILE="$CONFIG_DIR/output_base.txt"
LANGUAGE_FILE="$CONFIG_DIR/language.txt"
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
START_LANGUAGE="${LECTURE_SLIDE_CAPTURE_LANGUAGE:-}"
if [[ -z "$START_LANGUAGE" && -f "$LANGUAGE_FILE" ]]; then
  START_LANGUAGE="$(cat "$LANGUAGE_FILE" 2>/dev/null || true)"
fi
if [[ "$START_LANGUAGE" == ko* || "$START_LANGUAGE" == KO* ]]; then
  date '+시작: %Y-%m-%d %H:%M:%S'
else
  date '+Started: %Y-%m-%d %H:%M:%S'
fi
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

load_language() {
  local value="${LECTURE_SLIDE_CAPTURE_LANGUAGE:-}"
  if [[ -z "$value" && -f "$LANGUAGE_FILE" ]]; then
    value="$(cat "$LANGUAGE_FILE" 2>/dev/null || true)"
  fi
  case "$value" in
    ko*|KO*) printf 'ko' ;;
    *) printf 'en' ;;
  esac
}

t() {
  local en="$1"
  local ko="${2:-$1}"
  if [[ "$LANGUAGE" == "ko" ]]; then
    printf '%s' "$ko"
  else
    printf '%s' "$en"
  fi
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

LANGUAGE="$(load_language)"

prompt_menu() {
  local current="$1"
  local message
  message="$(t "Current base output folder:\n$current\n\nA timestamped session folder will be created under this path when capture starts." "현재 저장 기본 경로:\n$current\n\n캡처 시작 시 이 경로 아래에 실행 시각 폴더가 자동 생성됩니다.")"
  local cancel_label open_label choose_label list_label start_label
  cancel_label="$(t "Cancel" "취소")"
  open_label="$(t "Open in Finder" "Finder에서 열기")"
  choose_label="$(t "Choose Folder" "경로 지정")"
  list_label="$(t "List Windows" "창 목록 보기")"
  start_label="$(t "Start Capture" "캡처 시작")"
  local result
  result=$(run_osascript "try
  set picked to button returned of (display dialog $(as_applescript_string "$message") with title \"Lecture Slide Capture\" buttons {$(as_applescript_string "$cancel_label"), $(as_applescript_string "$open_label"), $(as_applescript_string "$choose_label"), $(as_applescript_string "$list_label"), $(as_applescript_string "$start_label")} default button $(as_applescript_string "$start_label") cancel button $(as_applescript_string "$cancel_label"))
  return picked
on error number -128
  return \"__CANCEL__\"
end try" 2>/dev/null) || result=""
  if [[ -z "$result" ]]; then
    echo >&2
    printf '%s %s\n' "$(t "Current base output folder:" "현재 저장 기본 경로:")" "$current" >&2
    echo "$(t "Menu:" "메뉴:")" >&2
    echo "  1) $start_label" >&2
    echo "  2) $list_label" >&2
    echo "  3) $choose_label" >&2
    echo "  4) $open_label" >&2
    echo "  5) $cancel_label" >&2
    echo >&2
    read -r -p "$(t "Input [1=Start Capture, 2=List Windows, 3=Choose Folder, 4=Open in Finder, 5=Cancel]: " "입력 [1=캡처 시작, 2=창 목록 보기, 3=경로 지정, 4=Finder에서 열기, 5=취소]: ")" text_choice || text_choice="5"
    case "$text_choice" in
      1) result="$start_label" ;;
      2) result="$list_label" ;;
      3) result="$choose_label" ;;
      4) result="$open_label" ;;
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
  local prompt
  prompt="$(t "Choose the base folder for captured slides." "캡처본 저장 기본 폴더를 선택하세요.")"
  picked=$(run_osascript "try
  set defaultLocation to POSIX file $(as_applescript_string "$current")
  set pickedFolder to choose folder with prompt $(as_applescript_string "$prompt") default location defaultLocation
  return POSIX path of pickedFolder
on error number -128
  return \"__CANCEL__\"
end try" 2>/dev/null) || picked=""

  if [[ -z "$picked" ]]; then
    echo
    read -r -e -p "$(t "Enter the new base output folder: " "새 저장 기본 경로를 입력하세요: ")" picked || picked=""
  fi

  if [[ -n "$picked" && "$picked" != "__CANCEL__" ]]; then
    save_output_base "$picked"
    printf '%s %s\n' "$(t "[settings] Base output folder changed:" "[설정] 저장 기본 경로를 변경했습니다:")" "$(load_output_base)"
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
  read -r -p "$(t "Press Enter to close this window..." "엔터를 누르면 이 창을 닫습니다...")" _ || true
  exit "$status"
}

ensure_python() {
  if [[ -z "$PYTHON_BIN" ]]; then
    echo "$(t "[error] Could not find python3." "[오류] python3 를 찾지 못했습니다.")"
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
    printf '%s %s\n' "$(t "[info] Some required Python packages are missing:" "[안내] 필요한 Python 패키지가 일부 없습니다:")" "$missing_modules"
    read -r -p "$(t "Install them now? [Y/n] " "지금 자동으로 설치할까요? [Y/n] ")" INSTALL_REPLY || INSTALL_REPLY="Y"
    INSTALL_REPLY="${INSTALL_REPLY:-Y}"
    case "$INSTALL_REPLY" in
      [Nn]*)
        echo
        echo "$(t "Install with this command, then run again:" "다음 명령으로 설치한 뒤 다시 실행하세요:")"
        echo "  python3 -m pip install --user -r \"$REQ_PATH\""
        pause_and_exit 1
        ;;
      *)
        echo
        echo "$(t "Installing packages..." "패키지를 설치합니다...")"
        "$PYTHON_BIN" -m pip install --user -r "$REQ_PATH" || pause_and_exit 1
        ;;
    esac
  fi
}

show_window_list() {
  echo >&2
  echo "$(t "[Current Chrome windows]" "[현재 Chrome 창 목록]")" >&2
  "$PYTHON_BIN" "$SCRIPT_PATH" --capture-source window --window-owner "Google Chrome" --list-windows --language "$LANGUAGE" >&2
  local status=$?
  if [[ "$status" -ne 0 ]]; then
    echo >&2
    echo "$(t "[info] No match for Google Chrome, retrying with Chrome." "[안내] Google Chrome 기준으로 찾지 못해 Chrome 기준으로 다시 시도합니다.")" >&2
    "$PYTHON_BIN" "$SCRIPT_PATH" --capture-source window --window-owner "Chrome" --list-windows --language "$LANGUAGE" >&2 || true
  fi
  echo >&2
}

choose_window_id_text() {
  local reply=""
  while true; do
    show_window_list
    read -r -p "$(t "Enter a window number or window ID. Enter=auto-select, r=refresh, q=cancel: " "창 번호 또는 창 ID를 입력하세요. Enter=자동 선택, r=목록 새로고침, q=취소: ")" reply || reply="q"
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
        echo "$(t "[info] Enter a numeric candidate number, a real window ID, or press Enter." "[안내] 숫자 후보 번호 또는 실제 창 ID를 입력하거나 Enter를 누르세요.")" >&2
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
    "__CANCEL__"|"Cancel"|"취소")
      exit 0
      ;;
    "Open in Finder"|"Finder에서 열기")
      open_output_base_in_finder "$CURRENT_OUTPUT_BASE"
      ;;
    "Choose Folder"|"경로 지정")
      choose_output_base "$CURRENT_OUTPUT_BASE"
      ;;
    "List Windows"|"창 목록 보기")
      show_window_list
      read -r -p "$(t "Press Enter to return to the menu..." "엔터를 누르면 메뉴로 돌아갑니다...")" _ || true
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
printf '%s %s\n' "$(t "[output]" "[저장]")" "$OUTPUT_DIR"

declare -a cmd
cmd=("$PYTHON_BIN" "$SCRIPT_PATH"
  --output "$OUTPUT_DIR"
  --capture-source window
  --window-owner "Google Chrome"
  --window-backend auto
  --preview
  --mode slide
  --make-pdf
  --language "$LANGUAGE"
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
  printf '%s %s\n' "$(t "[done] Output:" "[완료] 저장 위치:")" "$OUTPUT_DIR"
else
  if [[ "$LANGUAGE" == "ko" ]]; then
    printf '[종료] 프로그램이 상태 코드 %s 로 끝났습니다.\n' "$STATUS"
  else
    printf '[exit] Program ended with status code %s.\n' "$STATUS"
  fi
fi

pause_and_exit "$STATUS"
