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
DEFAULT_OUTPUT_BASE="$HOME/Desktop/lecture_captures"
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
  read -r -p "엔터를 누르면 이 창을 닫습니다..." _ || true
  exit "$status"
}

print_header() {
  echo "=============================================="
  echo " Lecture Slide Capture"
  echo "=============================================="
  echo "모드: $MODE"
  echo "저장 기본 경로: $OUTPUT_BASE"
  echo "이번 세션 폴더: $OUTPUT_DIR"
  if [[ -n "$WINDOW_ID" ]]; then
    echo "대상 창 ID: $WINDOW_ID"
  elif [[ "$MODE" == "capture" ]]; then
    echo "대상 창: 지금 목록에서 선택"
  else
    echo "대상 창 ID: 자동 선택"
  fi
  echo
}

if [[ -z "$PYTHON_BIN" ]]; then
  echo "[오류] python3 를 찾지 못했습니다."
  echo "Python 3 설치 후 다시 실행하세요."
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
  echo "[안내] 필요한 Python 패키지가 일부 없습니다: $missing_modules"
  read -r -p "지금 자동으로 설치할까요? [Y/n] " INSTALL_REPLY || INSTALL_REPLY="Y"
  INSTALL_REPLY="${INSTALL_REPLY:-Y}"
  case "$INSTALL_REPLY" in
    [Nn]*)
      echo
      echo "다음 명령으로 설치한 뒤 다시 실행하세요:"
      echo "  python3 -m pip install --user -r \"$REQ_PATH\""
      pause_and_exit 1
      ;;
    *)
      echo
      echo "패키지를 설치합니다..."
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

printf '[실행] '
printf '%q ' "${cmd[@]}"
printf '\n\n'

set +e
"${cmd[@]}"
STATUS=$?
set -e

echo
if [[ "$STATUS" -eq 0 ]]; then
  if [[ "$MODE" == "capture" ]]; then
    echo "[완료] 저장 위치: $OUTPUT_DIR"
  else
    echo "[완료] 창 목록 표시를 마쳤습니다."
  fi
else
  echo "[종료] 프로그램이 상태 코드 $STATUS 로 끝났습니다."
fi

pause_and_exit "$STATUS"
