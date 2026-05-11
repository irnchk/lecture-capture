#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import ctypes
import io
import os
import platform
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


APP_ID = "LectureSlideCapture"
SCRIPT_PATH = Path(__file__).resolve()
if getattr(sys, "frozen", False):
    RES_DIR = Path(getattr(sys, "_MEIPASS", SCRIPT_PATH.parent))
    APP_CONTENTS = RES_DIR.parent
else:
    RES_DIR = SCRIPT_PATH.parent
    APP_CONTENTS = RES_DIR.parent
REQ_PATH = RES_DIR / "requirements.txt"


def platform_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        return base / APP_ID
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_ID
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / APP_ID


def platform_log_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or (Path.home() / "AppData" / "Local"))
        return base / APP_ID / "Logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP_ID
    return Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / APP_ID / "logs"


CONFIG_DIR = platform_config_dir()
OUTPUT_BASE_FILE = CONFIG_DIR / "output_base.txt"
DEFAULT_OUTPUT_BASE = (
    Path.home() / "Documents" / "Lecture Slide Capture"
    if sys.platform == "win32"
    else Path.home() / ".hermes" / "workspace" / "NoteSources"
)
LOG_DIR = platform_log_dir()
GUI_LOG_PATH = LOG_DIR / "gui_session.log"

BASE_REQUIRED_MODULES = [
    "cv2",
    "mss",
    "skimage",
    "PIL",
    "img2pdf",
    "numpy",
]

MAC_REQUIRED_MODULES = [
    "Quartz",
    "AppKit",
    "Foundation",
    "ScreenCaptureKit",
]

BASE_RUNTIME_IMPORT_MODULES = [
    "cv2",
    "numpy",
    "mss",
    "skimage.metrics",
    "PIL",
    "img2pdf",
]


def required_modules() -> list[str]:
    if sys.platform == "darwin":
        return [*BASE_REQUIRED_MODULES, *MAC_REQUIRED_MODULES]
    return list(BASE_REQUIRED_MODULES)


def runtime_import_modules() -> list[str]:
    if sys.platform == "darwin":
        return [*BASE_RUNTIME_IMPORT_MODULES, *MAC_REQUIRED_MODULES]
    return list(BASE_RUNTIME_IMPORT_MODULES)


def enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

COMMON_PYTHON_CANDIDATES = [
    "/usr/local/bin/python3",
    "/opt/homebrew/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
    "/usr/bin/python3",
]


def normalize_output_base(value: str) -> Path:
    text = value.strip() if value else ""
    candidate = Path(text).expanduser() if text else DEFAULT_OUTPUT_BASE
    return candidate


def load_output_base() -> Path:
    try:
        if OUTPUT_BASE_FILE.exists():
            return normalize_output_base(OUTPUT_BASE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return DEFAULT_OUTPUT_BASE


def save_output_base(value: Path) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_BASE_FILE.write_text(str(value), encoding="utf-8")


def discover_missing_modules() -> list[str]:
    if getattr(sys, "frozen", False):
        return []

    missing: list[str] = []
    for module_name in required_modules():
        if importlib.util.find_spec(module_name) is None:
            missing.append(module_name)
    return missing


def arm64_machine_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/arch", "-arm64", "/usr/bin/true"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0


def probe_python_runtime(python_bin: str, force_arm64: bool = False) -> Optional[dict[str, Any]]:
    try:
        probe_script = "\n".join(
            [
                "import importlib",
                "import json",
                "import platform",
                "import sys",
                f"mods = {runtime_import_modules()!r}",
                "failures = {}",
                "for name in mods:",
                "    try:",
                "        importlib.import_module(name)",
                "    except Exception as exc:",
                "        failures[name] = repr(exc)",
                "print(json.dumps({'ok': not failures, 'failures': failures, 'machine': platform.machine(), 'exe': sys.executable}))",
            ]
        )
        command = [
            python_bin,
            "-c",
            probe_script,
        ]
        if force_arm64:
            command = ["/usr/bin/arch", "-arm64", *command]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None
    try:
        import json

        parsed = json.loads(result.stdout.strip() or "{}")
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def candidate_python_bins() -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    preferred = os.environ.get("PYTHON_BIN")
    dynamic_candidates = [preferred, shutil.which("python3"), sys.executable, *COMMON_PYTHON_CANDIDATES]
    for candidate in dynamic_candidates:
        if not candidate:
            continue
        path = str(Path(candidate).expanduser())
        if path in seen:
            continue
        if not Path(path).exists():
            continue
        seen.add(path)
        candidates.append(path)
    return candidates


def maybe_reexec_with_usable_python() -> bool:
    if sys.platform != "darwin":
        return False

    if os.environ.get("LECTURE_SLIDE_CAPTURE_REEXEC") == "1":
        return False

    current_path = str(Path(sys.executable).resolve())
    current_machine = platform.machine()
    arm64_available = arm64_machine_available()

    if arm64_available:
        current_arm64_probe = probe_python_runtime(sys.executable, force_arm64=True)
        if current_arm64_probe and current_arm64_probe.get("ok") is True:
            if current_machine != "arm64":
                os.environ["LECTURE_SLIDE_CAPTURE_REEXEC"] = "1"
                os.execv("/usr/bin/arch", ["arch", "-arm64", sys.executable, __file__, *sys.argv[1:]])
            return False

    current_probe = probe_python_runtime(sys.executable, force_arm64=False)
    if current_probe and current_probe.get("ok") is True:
        return False

    for candidate in candidate_python_bins():
        try:
            resolved_candidate = str(Path(candidate).resolve())
        except Exception:
            resolved_candidate = candidate
        if resolved_candidate == current_path:
            continue
        if arm64_available:
            arm64_probe = probe_python_runtime(candidate, force_arm64=True)
            if arm64_probe and arm64_probe.get("ok") is True:
                os.environ["LECTURE_SLIDE_CAPTURE_REEXEC"] = "1"
                os.execv("/usr/bin/arch", ["arch", "-arm64", candidate, __file__, *sys.argv[1:]])

        probe = probe_python_runtime(candidate, force_arm64=False)
        if probe and probe.get("ok") is True:
            os.environ["LECTURE_SLIDE_CAPTURE_REEXEC"] = "1"
            os.execv(candidate, [candidate, __file__, *sys.argv[1:]])
    return False


def get_python_bin() -> str:
    if sys.executable:
        return sys.executable
    found = shutil.which("python3")
    if found:
        return found
    raise RuntimeError("python3 실행 파일을 찾지 못했습니다.")


def build_install_command() -> str:
    command_parts = [get_python_bin(), "-m", "pip", "install", "--user", "-r", str(REQ_PATH)]
    if sys.platform == "win32":
        return subprocess.list2cmdline(command_parts)

    base_command = " ".join(shlex.quote(part) for part in command_parts)
    if sys.platform == "darwin" and arm64_machine_available():
        return f"/usr/bin/arch -arm64 {base_command}"
    return base_command


def show_long_message(parent: tk.Misc, title: str, message: str, details: str) -> None:
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent)
    dialog.grab_set()
    dialog.geometry("900x560")

    outer = ttk.Frame(dialog, padding=18)
    outer.pack(fill="both", expand=True)

    ttk.Label(outer, text=message, justify="left", wraplength=820).pack(anchor="w")

    text = scrolledtext.ScrolledText(outer, wrap="word", height=24)
    text.pack(fill="both", expand=True, pady=(12, 0))
    text.insert("1.0", details)
    text.configure(state="disabled")

    button_row = ttk.Frame(outer)
    button_row.pack(fill="x", pady=(12, 0))
    ttk.Button(button_row, text="닫기", command=dialog.destroy).pack(side="right")

    dialog.wait_window()


def clear_root_content(root: tk.Tk) -> None:
    for child in root.winfo_children():
        try:
            child.destroy()
        except Exception:
            pass


def present_root_window(root: tk.Tk, width: int, height: int) -> None:
    root.update_idletasks()
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    pos_x = max(80, int((screen_w - width) / 2))
    pos_y = max(80, int((screen_h - height) / 3))
    root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
    root.deiconify()
    root.lift()
    try:
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))
    except Exception:
        pass
    try:
        root.focus_force()
    except Exception:
        pass


def show_bootstrap_screen(root: tk.Tk, message: str) -> None:
    clear_root_content(root)
    root.title("Lecture Slide Capture")
    root.configure(background="#f5f1e8")
    root.minsize(520, 220)

    outer = tk.Frame(root, bg="#f5f1e8", padx=24, pady=24)
    outer.pack(fill="both", expand=True)

    tk.Label(
        outer,
        text="Lecture Slide Capture",
        bg="#f5f1e8",
        fg="#1f2937",
        font=("Helvetica", 18, "bold"),
        anchor="w",
        justify="left",
    ).pack(fill="x", anchor="w")

    tk.Label(
        outer,
        text=message,
        bg="#f5f1e8",
        fg="#4b5563",
        font=("Helvetica", 12),
        anchor="w",
        justify="left",
        wraplength=560,
        pady=14,
    ).pack(fill="x", anchor="w")

    present_root_window(root, 620, 260)
    root.update()


def show_error_screen(root: tk.Tk, title: str, message: str, details: str) -> None:
    clear_root_content(root)
    root.title(title)
    root.configure(background="#f5f1e8")
    root.minsize(760, 480)

    outer = tk.Frame(root, bg="#f5f1e8", padx=20, pady=20)
    outer.pack(fill="both", expand=True)

    tk.Label(
        outer,
        text=title,
        bg="#f5f1e8",
        fg="#7f1d1d",
        font=("Helvetica", 17, "bold"),
        anchor="w",
        justify="left",
    ).pack(fill="x", anchor="w")

    tk.Label(
        outer,
        text=message,
        bg="#f5f1e8",
        fg="#4b5563",
        font=("Helvetica", 12),
        anchor="w",
        justify="left",
        wraplength=760,
        pady=12,
    ).pack(fill="x", anchor="w")

    text = scrolledtext.ScrolledText(
        outer,
        wrap="word",
        height=18,
        font=("Menlo", 11),
        background="#fffdf7",
        foreground="#111827",
    )
    text.pack(fill="both", expand=True)
    text.insert("1.0", details)
    text.configure(state="disabled")

    button_row = tk.Frame(outer, bg="#f5f1e8", pady=12)
    button_row.pack(fill="x")

    def copy_details() -> None:
        try:
            root.clipboard_clear()
            root.clipboard_append(details)
        except Exception:
            pass

    tk.Button(button_row, text="내용 복사", command=copy_details, padx=14, pady=6).pack(side="left")
    tk.Button(button_row, text="닫기", command=root.destroy, padx=16, pady=6).pack(side="right")

    present_root_window(root, 900, 620)


def write_install_terminal_script(install_command: str, missing_text: str) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    script_path = CONFIG_DIR / "show_install_in_terminal.command"
    script_text = f"""#!/bin/bash
set -u

INSTALL_CMD={shlex.quote(install_command)}
MISSING_TEXT={shlex.quote(missing_text)}

clear
echo "=============================================="
echo " Lecture Slide Capture"
echo "=============================================="
echo
echo "필수 패키지가 아직 설치되지 않았습니다."
echo
echo "누락 항목:"
printf '%s\\n' "$MISSING_TEXT"
echo
echo "아래 명령을 그대로 실행하세요:"
echo
printf '%s\\n\\n' "$INSTALL_CMD"
if command -v pbcopy >/dev/null 2>&1; then
  printf '%s' "$INSTALL_CMD" | pbcopy
  echo "(설치 명령을 클립보드에 복사했습니다.)"
  echo
fi
read -r -p "엔터를 누르면 이 창을 닫습니다..." _
"""
    script_path.write_text(script_text, encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


def open_install_command_in_terminal(install_command: str, missing_text: str) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["cmd.exe", "/k", install_command])
        return
    if sys.platform != "darwin":
        subprocess.Popen(["sh", "-lc", install_command])
        return

    script_path = write_install_terminal_script(install_command, missing_text)
    subprocess.Popen(["open", str(script_path)])


def open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
        return
    subprocess.run(["xdg-open", str(path)], check=False)


def show_install_screen(root: tk.Tk, missing_text: str, install_command: str, clipboard_ready: bool) -> None:
    clear_root_content(root)
    root.title("Lecture Slide Capture")
    root.configure(background="#f5f1e8")
    root.geometry("860x420")
    root.minsize(760, 380)

    outer = tk.Frame(root, bg="#f5f1e8", padx=22, pady=22)
    outer.pack(fill="both", expand=True)

    tk.Label(
        outer,
        text="캡처에 필요한 Python 패키지가 아직 설치되지 않았습니다.",
        bg="#f5f1e8",
        fg="#1f2937",
        font=("Helvetica", 15, "bold"),
        anchor="w",
        justify="left",
    ).pack(fill="x", anchor="w")

    tk.Label(
        outer,
        text=f"누락 항목: {missing_text}",
        bg="#f5f1e8",
        fg="#374151",
        font=("Helvetica", 12),
        anchor="w",
        justify="left",
        wraplength=720,
        pady=10,
    ).pack(fill="x", anchor="w")

    note_text = (
        "앱 안에서는 자동 설치를 진행하지 않습니다.\n"
        "아래 명령을 Terminal에서 직접 실행한 뒤 앱을 다시 열어 주세요.\n"
        "원하면 Terminal 창을 자동으로 열어 안내만 표시할 수도 있습니다."
    )
    if clipboard_ready:
        note_text += "\n설치 명령은 클립보드에도 복사해 두었습니다."

    tk.Label(
        outer,
        text=note_text,
        bg="#f5f1e8",
        fg="#4b5563",
        font=("Helvetica", 12),
        anchor="w",
        justify="left",
        wraplength=720,
    ).pack(fill="x", anchor="w")

    command_box = tk.Text(
        outer,
        height=4,
        wrap="word",
        bg="#fffdf7",
        fg="#111827",
        relief="solid",
        borderwidth=1,
        padx=10,
        pady=10,
        font=("Menlo", 11),
    )
    command_box.pack(fill="x", expand=False, pady=(14, 0))
    command_box.insert("1.0", install_command)
    command_box.tag_add("all", "1.0", "end")
    command_box.configure(state="disabled")

    footer = tk.Label(
        outer,
        text="1. Terminal 열기  2. 명령 실행  3. 설치 완료 후 앱 다시 실행",
        bg="#f5f1e8",
        fg="#6b7280",
        font=("Helvetica", 11),
        anchor="w",
        justify="left",
        pady=12,
    )
    footer.pack(fill="x", anchor="w")

    def copy_command() -> None:
        try:
            root.clipboard_clear()
            root.clipboard_append(install_command)
        except Exception:
            pass

    button_row = tk.Frame(outer, bg="#f5f1e8")
    button_row.pack(fill="x")
    tk.Button(
        button_row,
        text="명령 복사",
        command=copy_command,
        padx=14,
        pady=6,
    ).pack(side="left")
    tk.Button(
        button_row,
        text="Terminal에서 열기",
        command=lambda: open_install_command_in_terminal(install_command, missing_text),
        padx=14,
        pady=6,
    ).pack(side="left", padx=(8, 0))
    tk.Button(
        button_row,
        text="닫기",
        command=root.destroy,
        padx=16,
        pady=6,
    ).pack(side="right")

    root.bind("<Escape>", lambda _event: root.destroy())
    width = max(760, root.winfo_reqwidth())
    height = max(320, root.winfo_reqheight())
    present_root_window(root, width, height)


class GuiLogStream(io.TextIOBase):
    def __init__(self, sink_queue: "queue.Queue[str]", log_file: io.TextIOBase, original: io.TextIOBase) -> None:
        self.sink_queue = sink_queue
        self.log_file = log_file
        self.original = original
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            try:
                self.log_file.write(text)
                self.log_file.flush()
            except Exception:
                pass
            try:
                self.original.write(text)
                self.original.flush()
            except Exception:
                pass
        self.sink_queue.put(text)
        return len(text)

    def flush(self) -> None:
        with self._lock:
            try:
                self.log_file.flush()
            except Exception:
                pass
            try:
                self.original.flush()
            except Exception:
                pass


@dataclass
class WindowRegionSelection:
    window: Dict[str, Any]
    roi: Tuple[int, int, int, int]
    source_size: Tuple[int, int]
    preview_bgr: Any


@dataclass
class ScreenRegionSelection:
    region: Dict[str, int]
    roi: Tuple[int, int, int, int]
    preview_bgr: Any


class RoiSelectorDialog:
    def __init__(self, parent: tk.Misc, image_bgr: Any, title: str, help_text: str) -> None:
        from PIL import Image, ImageTk

        self.Image = Image
        self.ImageTk = ImageTk
        self.parent = parent
        self.original_bgr = image_bgr
        self.help_text = help_text
        self.result: Optional[Tuple[int, int, int, int]] = None
        self.drag_start: Optional[Tuple[float, float]] = None
        self.rect_id: Optional[int] = None

        rgb = image_bgr[:, :, ::-1]
        pil_image = Image.fromarray(rgb)
        screen_w = max(1200, parent.winfo_screenwidth() - 200)
        screen_h = max(800, parent.winfo_screenheight() - 220)
        max_w = min(1600, screen_w)
        max_h = min(1000, screen_h)
        self.scale = min(max_w / float(pil_image.width), max_h / float(pil_image.height), 1.0)
        if self.scale < 1.0:
            display_size = (
                max(1, int(round(pil_image.width * self.scale))),
                max(1, int(round(pil_image.height * self.scale))),
            )
            pil_image = pil_image.resize(display_size, Image.Resampling.LANCZOS)
        self.display_image = pil_image
        self.photo = self.ImageTk.PhotoImage(self.display_image)
        self.viewport_width = min(self.display_image.width, max(680, parent.winfo_screenwidth() - 220))
        self.viewport_height = min(self.display_image.height, max(420, parent.winfo_screenheight() - 320))

        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.transient(parent)
        self.top.grab_set()
        self.top.resizable(True, True)

        outer = ttk.Frame(self.top, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(
            outer,
            text=help_text,
            justify="left",
            wraplength=min(1200, self.viewport_width),
        ).grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="드래그해서 슬라이드 영역을 선택하세요.")
        ttk.Label(outer, textvariable=self.status_var, foreground="#355c7d").grid(
            row=1,
            column=0,
            sticky="w",
            pady=(6, 10),
        )

        canvas_host = ttk.Frame(outer)
        canvas_host.grid(row=2, column=0, sticky="nsew")
        canvas_host.columnconfigure(0, weight=1)
        canvas_host.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(
            canvas_host,
            width=self.viewport_width,
            height=self.viewport_height,
            background="#0f172a",
            cursor="crosshair",
            highlightthickness=1,
            highlightbackground="#94a3b8",
            xscrollincrement=1,
            yscrollincrement=1,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.configure(
            scrollregion=(0, 0, self.display_image.width, self.display_image.height),
        )
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

        v_scroll = ttk.Scrollbar(canvas_host, orient="vertical", command=self.canvas.yview)
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll = ttk.Scrollbar(canvas_host, orient="horizontal", command=self.canvas.xview)
        h_scroll.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)

        button_row = ttk.Frame(outer)
        button_row.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(button_row, text="초기화", command=self._reset).pack(side="left")
        ttk.Button(button_row, text="취소", command=self._cancel).pack(side="right")
        ttk.Button(button_row, text="확인", command=self._confirm).pack(side="right", padx=(0, 8))

        self.top.bind("<Escape>", lambda _event: self._cancel())
        self.top.bind("<Return>", lambda _event: self._confirm())
        self.top.bind("<KP_Enter>", lambda _event: self._confirm())
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.top.update_idletasks()
        dialog_width = min(parent.winfo_screenwidth() - 120, self.viewport_width + 60)
        dialog_height = min(parent.winfo_screenheight() - 120, self.viewport_height + 160)
        self.top.geometry(
            f"{dialog_width}x{dialog_height}+{max(60, parent.winfo_rootx() + 40)}+{max(60, parent.winfo_rooty() + 40)}"
        )
        self.canvas.focus_set()

    def _canvas_point(self, event: tk.Event) -> Tuple[float, float]:
        x = min(max(float(self.canvas.canvasx(event.x)), 0.0), float(self.display_image.width))
        y = min(max(float(self.canvas.canvasy(event.y)), 0.0), float(self.display_image.height))
        return x, y

    def _on_mousewheel(self, event: tk.Event) -> None:
        raw_delta = int(getattr(event, "delta", 0) or 0)
        if raw_delta == 0:
            delta = -1
        elif abs(raw_delta) < 120:
            delta = -1 * raw_delta
        else:
            delta = -1 * int(raw_delta / 120)
        self.canvas.yview_scroll(delta, "units")

    def _on_press(self, event: tk.Event) -> None:
        self.drag_start = self._canvas_point(event)
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
            self.rect_id = None

    def _on_drag(self, event: tk.Event) -> None:
        if self.drag_start is None:
            return
        x0, y0 = self.drag_start
        x1, y1 = self._canvas_point(event)
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            x0,
            y0,
            x1,
            y1,
            outline="#f97316",
            width=2,
        )
        width = abs(x1 - x0)
        height = abs(y1 - y0)
        self.status_var.set(f"선택 중: {int(round(width))} x {int(round(height))}")

    def _on_release(self, event: tk.Event) -> None:
        self._on_drag(event)

    def _reset(self) -> None:
        self.drag_start = None
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
            self.rect_id = None
        self.result = None
        self.status_var.set("드래그해서 슬라이드 영역을 선택하세요.")

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()

    def _confirm(self) -> None:
        if self.drag_start is None or self.rect_id is None:
            messagebox.showinfo("영역 선택", "먼저 드래그해서 캡처할 영역을 선택해 주세요.", parent=self.top)
            return

        coords = self.canvas.coords(self.rect_id)
        if len(coords) != 4:
            return
        x0, y0, x1, y1 = coords
        left = int(round(min(x0, x1) / self.scale))
        top = int(round(min(y0, y1) / self.scale))
        width = int(round(abs(x1 - x0) / self.scale))
        height = int(round(abs(y1 - y0) / self.scale))
        if width <= 0 or height <= 0:
            messagebox.showinfo("영역 선택", "너비와 높이가 1 이상인 영역을 선택해 주세요.", parent=self.top)
            return
        self.result = (left, top, width, height)
        self.top.destroy()

    def show(self) -> Optional[Tuple[int, int, int, int]]:
        self.top.wait_window()
        return self.result


class CaptureApp:
    def __init__(self, root: tk.Tk, capture_module: Any) -> None:
        from PIL import Image, ImageDraw, ImageTk

        self.root = root
        self.sc = capture_module
        self.window_mode_supported = bool(getattr(self.sc, "WINDOW_CAPTURE_SUPPORTED", False))
        self.window_backend_choices = tuple(getattr(self.sc, "WINDOW_BACKEND_CHOICES", ("auto",)))
        self.Image = Image
        self.ImageDraw = ImageDraw
        self.ImageTk = ImageTk

        self.window_candidates: list[Dict[str, Any]] = []
        self.window_selection: Optional[WindowRegionSelection] = None
        self.screen_selection: Optional[ScreenRegionSelection] = None
        self.preview_photo: Optional[Any] = None
        self.last_saved_photo: Optional[Any] = None
        self.capture_thread: Optional[threading.Thread] = None
        self.engine: Optional[Any] = None
        self.output_dir: Optional[Path] = None
        self.run_error: Optional[str] = None
        self.finish_handled = False
        self.last_saved_count = -1
        self.last_thumb_path: Optional[Path] = None
        self.close_requested = False
        self.is_shutdown = False
        self.gallery_window: Optional[tk.Toplevel] = None
        self.gallery_canvas: Optional[tk.Canvas] = None
        self.gallery_inner: Optional[ttk.Frame] = None
        self.gallery_window_id: Optional[int] = None
        self.gallery_snapshot: Optional[tuple[str, int, str]] = None
        self.gallery_thumb_refs: list[Any] = []
        self.saved_strip_frame: Optional[ttk.Frame] = None
        self.saved_strip_snapshot: Optional[tuple[str, int, str]] = None
        self.saved_strip_thumb_refs: list[Any] = []
        self.capture_started_at: Optional[datetime] = None
        self.gallery_header_var = tk.StringVar(value="아직 열린 세션이 없습니다.")
        self.pause_button_text = tk.StringVar(value="일시정지")

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_file = GUI_LOG_PATH.open("a", encoding="utf-8")
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        sys.stdout = GuiLogStream(self.log_queue, self.log_file, self.original_stdout)
        sys.stderr = GuiLogStream(self.log_queue, self.log_file, self.original_stderr)

        self.source_mode_var = tk.StringVar(value="window" if self.window_mode_supported else "screen")
        self.window_owner_var = tk.StringVar(value=str(getattr(self.sc, "DEFAULT_WINDOW_OWNER", "Google Chrome")))
        self.window_title_var = tk.StringVar(value="")
        self.window_backend_var = tk.StringVar(value="auto")
        self.output_base_var = tk.StringVar(value=str(load_output_base()))
        self.mode_var = tk.StringVar(value="slide")
        self.interval_var = tk.StringVar(value="0.60")
        self.make_pdf_var = tk.BooleanVar(value=True)
        self.keep_duplicates_var = tk.BooleanVar(value=False)
        self.pause_on_cursor_var = tk.BooleanVar(value=True)
        self.selection_summary_var = tk.StringVar(value="아직 캡처 영역이 선택되지 않았습니다.")
        self.session_status_var = tk.StringVar(value="대기 중")
        self.saved_count_var = tk.StringVar(value="0")
        self.duplicate_count_var = tk.StringVar(value="0")
        self.session_dir_var = tk.StringVar(value="-")
        self.last_saved_var = tk.StringVar(value="-")
        self.elapsed_var = tk.StringVar(value="00:00")

        self._configure_root()
        self._build_ui()
        self._sync_source_mode_ui()
        self._refresh_windows()
        self._append_log("Lecture Slide Capture GUI 준비 완료")
        self.root.after(180, self._tick)

    def _configure_root(self) -> None:
        self.palette = {
            "bg": "#fbfaf7",
            "panel": "#fffefa",
            "panel_alt": "#f8f6f1",
            "border": "#ded8cd",
            "text": "#1f2937",
            "muted": "#667085",
            "accent": "#d97706",
            "accent_light": "#fff7ed",
            "accent_dark": "#92400e",
            "success": "#15803d",
            "info": "#2563eb",
            "danger": "#b91c1c",
            "log_bg": "#ffffff",
            "log_fg": "#1f2937",
        }
        self.root.title("Lecture Slide Capture")
        self.root.geometry("1536x940")
        self.root.minsize(1280, 820)
        self.root.configure(background=self.palette["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=("SF Pro Text", 12), background=self.palette["bg"], foreground=self.palette["text"])
        style.configure("Root.TFrame", background=self.palette["bg"])
        style.configure("Panel.TFrame", background=self.palette["panel"])
        style.configure("Soft.TFrame", background=self.palette["panel_alt"])
        style.configure("Header.TLabel", font=("SF Pro Display", 25, "bold"), foreground=self.palette["text"], background=self.palette["bg"])
        style.configure("Subheader.TLabel", foreground=self.palette["muted"], background=self.palette["bg"])
        style.configure("PanelSubheader.TLabel", foreground=self.palette["muted"], background=self.palette["panel"])
        style.configure("Muted.TLabel", foreground=self.palette["muted"])
        style.configure(
            "Step.TLabelframe",
            background=self.palette["panel"],
            bordercolor=self.palette["border"],
            relief="solid",
        )
        style.configure(
            "Step.TLabelframe.Label",
            font=("SF Pro Display", 14, "bold"),
            foreground=self.palette["text"],
            background=self.palette["bg"],
        )
        style.configure(
            "Section.TLabelframe",
            background=self.palette["panel"],
            bordercolor=self.palette["border"],
            relief="solid",
        )
        style.configure(
            "Section.TLabelframe.Label",
            font=("SF Pro Text", 12, "bold"),
            foreground=self.palette["text"],
            background=self.palette["bg"],
        )
        style.configure("TLabel", background=self.palette["panel"], foreground=self.palette["text"])
        style.configure("TRadiobutton", background=self.palette["panel"], foreground=self.palette["text"])
        style.configure("TCheckbutton", background=self.palette["panel"], foreground=self.palette["text"])
        style.configure("TEntry", fieldbackground="#ffffff")
        style.configure("TCombobox", fieldbackground="#ffffff")
        style.configure("Ghost.TButton", padding=(12, 6))
        style.configure("Accent.TButton", font=("SF Pro Text", 12, "bold"), foreground="#ffffff", background=self.palette["accent"])
        style.map("Accent.TButton", background=[("active", "#f59e0b"), ("disabled", "#e5e7eb")], foreground=[("disabled", "#9ca3af")])
        style.configure("Danger.TButton", foreground=self.palette["danger"])

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, style="Root.TFrame")
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)

        app_header = ttk.Frame(shell, padding=(22, 18, 22, 8), style="Root.TFrame")
        app_header.grid(row=0, column=0, sticky="ew")
        app_header.columnconfigure(0, weight=1)
        ttk.Label(app_header, text="Lecture Slide Capture", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            app_header,
            text="강의 창 선택부터 슬라이드 영역 지정, 자동 저장과 PDF 생성까지 한 화면에서 관리합니다.",
            style="Subheader.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        scroll_host = ttk.Frame(shell, style="Root.TFrame")
        scroll_host.grid(row=1, column=0, sticky="nsew")
        scroll_host.columnconfigure(0, weight=1)
        scroll_host.rowconfigure(0, weight=1)

        self.main_canvas = tk.Canvas(
            scroll_host,
            background=self.palette["bg"],
            highlightthickness=0,
            bd=0,
        )
        self.main_canvas.grid(row=0, column=0, sticky="nsew")

        self.main_scrollbar = ttk.Scrollbar(scroll_host, orient="vertical", command=self.main_canvas.yview)
        self.main_scrollbar.grid(row=0, column=1, sticky="ns")
        self.main_canvas.configure(yscrollcommand=self.main_scrollbar.set)

        outer = ttk.Frame(self.main_canvas, padding=(22, 0, 22, 14), style="Root.TFrame")
        outer.columnconfigure(0, weight=0, minsize=470)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)
        self._canvas_window_id = self.main_canvas.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", self._on_scroll_content_configure)
        self.main_canvas.bind("<Configure>", self._on_main_canvas_configure)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

        setup_frame = ttk.Frame(outer, style="Root.TFrame")
        setup_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        setup_frame.columnconfigure(0, weight=1)

        preview_frame = ttk.Frame(outer, style="Root.TFrame")
        preview_frame.grid(row=0, column=1, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(2, weight=4)
        preview_frame.rowconfigure(4, weight=2)

        def make_card(
            parent: tk.Misc,
            title: str,
            subtitle: str = "",
            *,
            accent: str = "",
            step_number: Optional[str] = None,
            padding: Tuple[int, int, int, int] = (14, 10, 14, 14),
            **grid_options: Any,
        ) -> ttk.Frame:
            card = tk.Frame(
                parent,
                bg=self.palette["panel"],
                highlightbackground=self.palette["border"],
                highlightcolor=self.palette["border"],
                highlightthickness=1,
                bd=0,
            )
            card.grid(**grid_options)
            card.columnconfigure(0, weight=1)
            card.rowconfigure(1, weight=1)

            header_row = tk.Frame(card, bg=self.palette["panel"])
            header_row.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 0))
            header_row.columnconfigure(1, weight=1)
            if step_number:
                badge = tk.Canvas(header_row, width=34, height=34, bg=self.palette["panel"], highlightthickness=0, bd=0)
                badge.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 10))
                fill = accent or self.palette["accent"]
                badge.create_oval(2, 2, 32, 32, fill=fill, outline=fill)
                badge.create_text(17, 17, text=step_number, fill="#ffffff", font=("SF Pro Display", 15, "bold"))
                title_col = 1
            elif accent:
                tk.Frame(header_row, width=4, height=24, bg=accent, bd=0).grid(row=0, column=0, sticky="ns", padx=(0, 8))
                title_col = 1
            else:
                title_col = 0

            tk.Label(
                header_row,
                text=title,
                bg=self.palette["panel"],
                fg=self.palette["text"],
                font=("SF Pro Display", 14, "bold"),
                anchor="w",
            ).grid(row=0, column=title_col, sticky="w")
            if subtitle:
                tk.Label(
                    header_row,
                    text=subtitle,
                    bg=self.palette["panel"],
                    fg=self.palette["muted"],
                    font=("SF Pro Text", 11),
                    anchor="w",
                ).grid(row=1, column=title_col, sticky="w", pady=(2, 0))

            body = ttk.Frame(card, padding=padding, style="Panel.TFrame")
            body.grid(row=1, column=0, sticky="nsew")
            body.columnconfigure(0, weight=1)
            return body

        source_group = make_card(
            setup_frame,
            "1  소스",
            "강의가 표시되는 창 또는 화면 영역을 고릅니다.",
            accent=self.palette["info"],
            step_number="1",
            row=0,
            column=0,
            sticky="ew",
        )
        source_group.columnconfigure(1, weight=1)

        self.window_mode_button = tk.Radiobutton(
            source_group,
            text="Chrome 창 고정 캡처",
            variable=self.source_mode_var,
            value="window",
            command=self._sync_source_mode_ui,
            indicatoron=False,
            bg=self.palette["accent_light"],
            fg=self.palette["text"],
            activebackground=self.palette["accent_light"],
            selectcolor="#ffffff",
            relief="solid",
            bd=1,
            padx=12,
            pady=7,
            anchor="center",
        )
        self.window_mode_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.screen_mode_button = tk.Radiobutton(
            source_group,
            text="화면 영역 직접 캡처",
            variable=self.source_mode_var,
            value="screen",
            command=self._sync_source_mode_ui,
            indicatoron=False,
            bg="#ffffff",
            fg=self.palette["text"],
            activebackground="#f8fafc",
            selectcolor="#ffffff",
            relief="solid",
            bd=1,
            padx=12,
            pady=7,
            anchor="center",
        )
        self.screen_mode_button.grid(row=0, column=1, sticky="ew")

        ttk.Label(source_group, text="앱 이름 필터").grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.owner_entry = ttk.Entry(source_group, textvariable=self.window_owner_var)
        self.owner_entry.grid(row=1, column=1, sticky="ew", pady=(12, 0))
        ttk.Label(source_group, text="창 제목 필터").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.title_entry = ttk.Entry(source_group, textvariable=self.window_title_var)
        self.title_entry.grid(row=2, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(source_group, text="창 캡처 백엔드").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.backend_combo = ttk.Combobox(
            source_group,
            textvariable=self.window_backend_var,
            values=self.window_backend_choices,
            state="readonly",
        )
        self.backend_combo.grid(row=3, column=1, sticky="ew", pady=(8, 0))

        window_group = source_group
        button_row = ttk.Frame(window_group, style="Panel.TFrame")
        button_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.refresh_button = ttk.Button(button_row, text="창 목록 새로고침", command=self._refresh_windows)
        self.refresh_button.pack(side="left")

        self.window_listbox = tk.Listbox(
            window_group,
            height=4,
            activestyle="dotbox",
            exportselection=False,
            font=("SF Mono", 11),
            background="#ffffff",
            foreground=self.palette["text"],
            highlightthickness=1,
            highlightbackground=self.palette["border"],
            selectbackground=self.palette["accent"],
            selectforeground="#ffffff",
            relief="flat",
        )
        self.window_listbox.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.window_listbox.bind("<<ListboxSelect>>", self._on_window_selected)

        region_group = make_card(
            setup_frame,
            "2  영역",
            "슬라이드가 보이는 부분만 ROI로 지정합니다.",
            accent=self.palette["accent"],
            step_number="2",
            row=1,
            column=0,
            sticky="ew",
            pady=(12, 0),
        )
        region_group.columnconfigure(0, weight=1)
        ttk.Label(
            region_group,
            textvariable=self.selection_summary_var,
            wraplength=410,
            justify="left",
            style="PanelSubheader.TLabel",
        ).grid(row=0, column=0, sticky="ew")
        self.pick_region_button = ttk.Button(region_group, text="슬라이드 영역 선택", command=self._choose_region)
        self.pick_region_button.grid(row=1, column=0, sticky="ew", pady=(12, 0))

        capture_group = make_card(
            setup_frame,
            "3  캡처",
            "저장 위치와 감지 옵션을 확인한 뒤 시작합니다.",
            accent=self.palette["success"],
            step_number="3",
            row=2,
            column=0,
            sticky="ew",
            pady=(12, 0),
        )
        capture_group.columnconfigure(1, weight=1)
        capture_group.columnconfigure(3, weight=1)
        ttk.Label(capture_group, text="저장 기본 경로").grid(row=0, column=0, sticky="w")
        self.output_entry = ttk.Entry(capture_group, textvariable=self.output_base_var)
        self.output_entry.grid(row=0, column=1, sticky="ew", padx=(10, 8))
        ttk.Button(capture_group, text="찾아보기", command=self._browse_output_dir).grid(row=0, column=2)
        ttk.Label(capture_group, text="감지 모드").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Combobox(
            capture_group,
            textvariable=self.mode_var,
            values=("slide", "detailed"),
            state="readonly",
        ).grid(row=1, column=1, sticky="ew", padx=(10, 8), pady=(10, 0))
        ttk.Label(capture_group, text="간격").grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Entry(capture_group, textvariable=self.interval_var, width=8).grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=(10, 0))
        ttk.Checkbutton(capture_group, text="종료 시 PDF 생성", variable=self.make_pdf_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Checkbutton(capture_group, text="중복 슬라이드도 유지", variable=self.keep_duplicates_var).grid(
            row=2, column=2, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Checkbutton(capture_group, text="커서가 ROI 안에 있으면 일시정지", variable=self.pause_on_cursor_var).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )

        ttk.Label(preview_frame, text="실시간 상태", style="Subheader.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))

        metrics_frame = ttk.Frame(preview_frame, style="Root.TFrame")
        metrics_frame.grid(row=1, column=0, sticky="ew")
        for col in range(4):
            metrics_frame.columnconfigure(col, weight=1)

        def metric_card(parent: tk.Misc, column: int, title: str, variable: tk.StringVar, detail: str, color: str) -> None:
            card = tk.Frame(
                parent,
                bg="#ffffff",
                highlightbackground=self.palette["border"],
                highlightthickness=1,
                bd=0,
            )
            card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 10, 0))
            tk.Label(
                card,
                text=title,
                bg="#ffffff",
                fg=self.palette["muted"],
                font=("SF Pro Text", 10, "bold"),
                padx=12,
                pady=3,
            ).pack(anchor="w")
            tk.Label(
                card,
                textvariable=variable,
                bg="#ffffff",
                fg=color,
                font=("SF Pro Display", 22, "bold"),
                padx=12,
                pady=2,
            ).pack(anchor="w")
            tk.Label(
                card,
                text=detail,
                bg="#ffffff",
                fg=self.palette["muted"],
                font=("SF Pro Text", 11),
                padx=12,
                pady=3,
            ).pack(anchor="w")

        metric_card(metrics_frame, 0, "상태", self.session_status_var, "준비되었습니다.", self.palette["info"])
        metric_card(metrics_frame, 1, "저장된 슬라이드", self.saved_count_var, "이번 세션", self.palette["success"])
        metric_card(metrics_frame, 2, "중복으로 건너뜀", self.duplicate_count_var, "이번 세션", self.palette["accent_dark"])
        metric_card(metrics_frame, 3, "경과 시간", self.elapsed_var, "mm:ss", self.palette["text"])

        preview_grid = ttk.Frame(preview_frame, style="Root.TFrame")
        preview_grid.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        preview_grid.columnconfigure(0, weight=1)
        preview_grid.columnconfigure(1, weight=1)
        preview_grid.rowconfigure(0, weight=1)

        selection_group = make_card(
            preview_grid,
            "선택 영역 미리보기",
            padding=(14, 10, 14, 14),
            row=0,
            column=0,
            sticky="nsew",
            padx=(0, 8),
        )
        selection_group.columnconfigure(0, weight=1)
        selection_group.rowconfigure(0, weight=1)
        self.selection_image_label = ttk.Label(selection_group, text="아직 미리보기가 없습니다.", anchor="center")
        self.selection_image_label.grid(row=0, column=0, sticky="nsew")

        saved_group = make_card(
            preview_grid,
            "Latest Slide",
            padding=(14, 10, 14, 14),
            row=0,
            column=1,
            sticky="nsew",
            padx=(8, 0),
        )
        saved_group.columnconfigure(0, weight=1)
        saved_group.rowconfigure(0, weight=1)
        self.saved_image_label = ttk.Label(saved_group, text="캡처가 시작되면 최근 저장된 슬라이드를 여기에 보여줍니다.", anchor="center")
        self.saved_image_label.grid(row=0, column=0, sticky="nsew")

        saved_strip_group = make_card(
            preview_frame,
            "Saved Slides",
            padding=(14, 10, 14, 14),
            row=3,
            column=0,
            sticky="ew",
            pady=(12, 0),
        )
        self.saved_strip_frame = ttk.Frame(saved_strip_group, style="Panel.TFrame")
        self.saved_strip_frame.grid(row=0, column=0, sticky="ew")
        self._refresh_saved_strip(force=True)

        logs_frame = make_card(
            preview_frame,
            "Session Log",
            padding=(14, 10, 14, 14),
            row=4,
            column=0,
            sticky="nsew",
            pady=(12, 0),
        )
        logs_frame.columnconfigure(0, weight=1)
        logs_frame.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            logs_frame,
            wrap="word",
            font=("SF Mono", 11),
            background=self.palette["log_bg"],
            foreground=self.palette["log_fg"],
            insertbackground=self.palette["text"],
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=10,
            height=7,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("saved", foreground=self.palette["success"])
        self.log_text.tag_configure("wait", foreground=self.palette["info"])
        self.log_text.tag_configure("error", foreground=self.palette["danger"])
        self.log_text.configure(state="disabled")

        footer = ttk.Frame(shell, padding=(22, 10, 22, 18), style="Root.TFrame")
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(1, weight=1)
        ttk.Separator(footer, orient="horizontal").grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        self.open_folder_button = ttk.Button(footer, text="세션 폴더 열기", command=self._open_session_folder)
        self.open_folder_button.grid(row=1, column=0, sticky="w")
        ttk.Label(
            footer,
            text="영역을 먼저 선택한 뒤 캡처를 시작하세요. 진행 중에는 일시정지와 종료만 사용할 수 있습니다.",
            style="Subheader.TLabel",
        ).grid(row=1, column=1, sticky="w", padx=(14, 14))

        action_row = ttk.Frame(footer, style="Root.TFrame")
        action_row.grid(row=1, column=2, sticky="e")
        self.start_button = ttk.Button(action_row, text="Start Capture", style="Accent.TButton", command=self._start_capture)
        self.start_button.pack(side="left")
        self.pause_button = ttk.Button(
            action_row,
            textvariable=self.pause_button_text,
            command=self._toggle_pause_capture,
            state="disabled",
        )
        self.pause_button.pack(side="left", padx=(8, 0))
        self.finish_button = ttk.Button(action_row, text="Finish", style="Danger.TButton", command=self._finish_capture, state="disabled")
        self.finish_button.pack(side="left", padx=(8, 0))
        self.view_slides_button = ttk.Button(action_row, text="캡처본 전체 보기", command=self._open_slides_gallery)
        self.view_slides_button.pack(side="left", padx=(8, 0))

    def _on_scroll_content_configure(self, _event: tk.Event) -> None:
        self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all"))

    def _on_main_canvas_configure(self, event: tk.Event) -> None:
        self.main_canvas.itemconfigure(self._canvas_window_id, width=event.width)

    @staticmethod
    def _widget_is_descendant(widget: Optional[tk.Misc], ancestor: tk.Misc) -> bool:
        current = widget
        while current is not None:
            if current == ancestor:
                return True
            current_name = getattr(current, "master", None)
            current = current_name
        return False

    def _on_mousewheel(self, event: tk.Event) -> None:
        if self.is_shutdown or not getattr(self, "main_canvas", None):
            return
        widget = self.root.winfo_containing(self.root.winfo_pointerx(), self.root.winfo_pointery())
        raw_delta = int(getattr(event, "delta", 0) or 0)
        if raw_delta == 0:
            delta = -1
        elif abs(raw_delta) < 120:
            delta = -1 * raw_delta
        else:
            delta = -1 * int(raw_delta / 120)

        if self.gallery_window is not None and widget is not None and self._widget_is_descendant(widget, self.gallery_window):
            if self.gallery_canvas is not None:
                self.gallery_canvas.yview_scroll(delta, "units")
            return

        if widget is not None and self._widget_is_descendant(widget, self.log_text):
            return
        self.main_canvas.yview_scroll(delta, "units")

    def _captured_slide_paths(self) -> list[Path]:
        if self.engine is not None and self.engine.saved_paths:
            return list(self.engine.saved_paths)
        if self.output_dir is not None and self.output_dir.exists():
            return sorted(self.output_dir.glob("slide_*.png"))
        return []

    def _toggle_pause_capture(self) -> None:
        if self.engine is None or self.capture_thread is None or not self.capture_thread.is_alive():
            return
        if self.engine.stopper.paused:
            self.engine.stopper.resume()
            self.pause_button_text.set("일시정지")
            self.session_status_var.set("캡처 중")
            self._append_log("[gui] 캡처를 재개했습니다.")
        else:
            self.engine.stopper.pause()
            self.pause_button_text.set("재개")
            self.session_status_var.set("일시정지됨")
            self._append_log("[gui] 캡처를 일시정지했습니다.")

    def _finish_capture(self) -> None:
        if self.engine is None:
            return
        self.engine.stopper.request_stop()
        self.session_status_var.set("종료 요청 중")
        self._append_log("[gui] 종료 요청을 보냈습니다.")
        self.pause_button.configure(state="disabled")
        self.finish_button.configure(state="disabled")

    def _open_image_path(self, path: Path) -> None:
        open_path(path)

    def _refresh_gallery_canvas(self, _event: Optional[tk.Event] = None) -> None:
        if self.gallery_canvas is None or self.gallery_inner is None:
            return
        self.gallery_canvas.configure(scrollregion=self.gallery_canvas.bbox("all"))

    def _resize_gallery_canvas(self, event: tk.Event) -> None:
        if self.gallery_canvas is None or self.gallery_window_id is None:
            return
        self.gallery_canvas.itemconfigure(self.gallery_window_id, width=event.width)

    def _close_gallery(self) -> None:
        if self.gallery_window is None:
            return
        try:
            self.gallery_window.destroy()
        except Exception:
            pass
        self.gallery_window = None
        self.gallery_canvas = None
        self.gallery_inner = None
        self.gallery_window_id = None
        self.gallery_snapshot = None
        self.gallery_thumb_refs = []

    def _refresh_slides_gallery(self, force: bool = False) -> None:
        if self.gallery_window is None or self.gallery_inner is None:
            return
        if not self.gallery_window.winfo_exists():
            self._close_gallery()
            return

        paths = self._captured_slide_paths()
        latest = str(paths[-1]) if paths else ""
        output_root = str(self.output_dir) if self.output_dir is not None else "-"
        snapshot = (output_root, len(paths), latest)
        if not force and snapshot == self.gallery_snapshot:
            return
        self.gallery_snapshot = snapshot

        for child in self.gallery_inner.winfo_children():
            child.destroy()
        self.gallery_thumb_refs = []

        self.gallery_header_var.set(f"세션 폴더: {output_root}\n저장된 슬라이드: {len(paths)}장")

        if not paths:
            ttk.Label(
                self.gallery_inner,
                text="아직 저장된 슬라이드가 없습니다.\n캡처를 시작하거나 잠시 기다려 주세요.",
                justify="center",
            ).grid(row=0, column=0, padx=20, pady=20, sticky="nsew")
            self._refresh_gallery_canvas()
            return

        columns = 3
        for col in range(columns):
            self.gallery_inner.columnconfigure(col, weight=1)

        for index, path in enumerate(paths):
            row = index // columns
            col = index % columns
            card = ttk.Frame(self.gallery_inner, padding=10, style="Panel.TFrame")
            card.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")
            card.columnconfigure(0, weight=1)

            try:
                image = self.Image.open(path).convert("RGB")
                image.thumbnail((260, 180), self.Image.Resampling.LANCZOS)
                photo = self.ImageTk.PhotoImage(image)
                self.gallery_thumb_refs.append(photo)
                image_label = ttk.Label(card, image=photo, anchor="center")
                image_label.grid(row=0, column=0, sticky="nsew")
                image_label.bind("<Button-1>", lambda _event, p=path: self._open_image_path(p))
            except Exception:
                ttk.Label(card, text="미리보기를 불러오지 못했습니다.", anchor="center").grid(
                    row=0, column=0, sticky="nsew", pady=(20, 20)
                )

            ttk.Label(
                card,
                text=f"{index + 1:02d}. {path.name}",
                justify="left",
                wraplength=260,
            ).grid(row=1, column=0, sticky="w", pady=(8, 0))
            ttk.Button(card, text="파일 열기", command=lambda p=path: self._open_image_path(p)).grid(
                row=2, column=0, sticky="w", pady=(8, 0)
            )

        self._refresh_gallery_canvas()

    def _open_slides_gallery(self) -> None:
        if self.gallery_window is not None and self.gallery_window.winfo_exists():
            self.gallery_window.deiconify()
            self.gallery_window.lift()
            self._refresh_slides_gallery(force=True)
            return

        self.gallery_window = tk.Toplevel(self.root)
        self.gallery_window.title("캡처본 전체 보기")
        self.gallery_window.geometry("980x760")
        self.gallery_window.minsize(760, 560)
        self.gallery_window.transient(self.root)
        self.gallery_window.protocol("WM_DELETE_WINDOW", self._close_gallery)

        outer = ttk.Frame(self.gallery_window, padding=16, style="Root.TFrame")
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        header = ttk.Frame(outer, style="Root.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="현재 캡처된 슬라이드", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.gallery_header_var, justify="left").grid(row=1, column=0, sticky="w", pady=(6, 0))
        header_buttons = ttk.Frame(header, style="Root.TFrame")
        header_buttons.grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Button(header_buttons, text="새로고침", command=lambda: self._refresh_slides_gallery(force=True)).pack(side="left")
        ttk.Button(header_buttons, text="세션 폴더 열기", command=self._open_session_folder).pack(side="left", padx=(8, 0))

        body = ttk.Frame(outer, style="Root.TFrame")
        body.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self.gallery_canvas = tk.Canvas(body, background=self.palette["bg"], highlightthickness=0, bd=0)
        self.gallery_canvas.grid(row=0, column=0, sticky="nsew")
        gallery_scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.gallery_canvas.yview)
        gallery_scrollbar.grid(row=0, column=1, sticky="ns")
        self.gallery_canvas.configure(yscrollcommand=gallery_scrollbar.set)

        self.gallery_inner = ttk.Frame(self.gallery_canvas, padding=4, style="Root.TFrame")
        self.gallery_window_id = self.gallery_canvas.create_window((0, 0), window=self.gallery_inner, anchor="nw")
        self.gallery_inner.bind("<Configure>", self._refresh_gallery_canvas)
        self.gallery_canvas.bind("<Configure>", self._resize_gallery_canvas)

        self._refresh_slides_gallery(force=True)

    def _refresh_saved_strip(self, force: bool = False) -> None:
        if self.saved_strip_frame is None:
            return

        paths = self._captured_slide_paths()
        latest = str(paths[-1]) if paths else ""
        output_root = str(self.output_dir) if self.output_dir is not None else "-"
        snapshot = (output_root, len(paths), latest)
        if not force and snapshot == self.saved_strip_snapshot:
            return
        self.saved_strip_snapshot = snapshot

        for child in self.saved_strip_frame.winfo_children():
            child.destroy()
        self.saved_strip_thumb_refs = []

        if not paths:
            ttk.Label(
                self.saved_strip_frame,
                text="아직 저장된 슬라이드가 없습니다.",
                style="PanelSubheader.TLabel",
            ).grid(row=0, column=0, sticky="w")
            return

        recent_paths = paths[-6:]
        for index, path in enumerate(recent_paths):
            cell = tk.Frame(
                self.saved_strip_frame,
                bg="#ffffff",
                highlightbackground=self.palette["border"],
                highlightthickness=1,
                bd=0,
            )
            cell.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 10, 0))
            self.saved_strip_frame.columnconfigure(index, weight=1)
            try:
                image = self.Image.open(path).convert("RGB")
                image.thumbnail((120, 78), self.Image.Resampling.LANCZOS)
                photo = self.ImageTk.PhotoImage(image)
                self.saved_strip_thumb_refs.append(photo)
                image_label = ttk.Label(cell, image=photo, anchor="center")
                image_label.grid(row=0, column=0, sticky="nsew", padx=6, pady=(6, 0))
                image_label.bind("<Button-1>", lambda _event, p=path: self._open_image_path(p))
            except Exception:
                ttk.Label(cell, text="미리보기 없음", anchor="center").grid(row=0, column=0, padx=6, pady=(16, 12))
            ttk.Label(
                cell,
                text=path.name[:18] + ("..." if len(path.name) > 18 else ""),
                anchor="center",
                style="PanelSubheader.TLabel",
            ).grid(row=1, column=0, sticky="ew", padx=6, pady=(4, 6))

    def _log_tag_for_line(self, line: str) -> str:
        lowered = line.lower()
        if "[saved]" in lowered or "저장" in line:
            return "saved"
        if "[wait]" in lowered or "[window-guard]" in lowered:
            return "wait"
        if "error" in lowered or "오류" in line or "traceback" in lowered:
            return "error"
        return ""

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        line = message.rstrip("\n")
        tag = self._log_tag_for_line(line)
        if tag:
            self.log_text.insert("end", line + "\n", tag)
        else:
            self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _drain_logs(self) -> None:
        chunks: list[str] = []
        while True:
            try:
                chunks.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return
        combined = "".join(chunks)
        if combined:
            self.log_text.configure(state="normal")
            for line in combined.splitlines(keepends=True):
                tag = self._log_tag_for_line(line)
                if tag:
                    self.log_text.insert("end", line, tag)
                else:
                    self.log_text.insert("end", line)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

    def _browse_output_dir(self) -> None:
        chosen = filedialog.askdirectory(
            title="저장 기본 경로 선택",
            initialdir=str(normalize_output_base(self.output_base_var.get())),
            parent=self.root,
        )
        if chosen:
            self.output_base_var.set(chosen)

    def _sync_source_mode_ui(self) -> None:
        if not self.window_mode_supported and self.source_mode_var.get() == "window":
            self.source_mode_var.set("screen")
        is_window_mode = self.source_mode_var.get() == "window"
        window_controls_enabled = is_window_mode and self.window_mode_supported
        state = "normal" if window_controls_enabled else "disabled"
        readonly_state = "readonly" if window_controls_enabled else "disabled"
        for widget in (self.owner_entry, self.title_entry, self.refresh_button):
            widget.configure(state=state)
        self.backend_combo.configure(state=readonly_state)
        self.window_listbox.configure(state=state)
        if hasattr(self, "window_mode_button") and hasattr(self, "screen_mode_button"):
            self.window_mode_button.configure(state="normal" if self.window_mode_supported else "disabled")
            self.window_mode_button.configure(
                bg=self.palette["accent_light"] if window_controls_enabled else "#ffffff",
                fg=self.palette["accent_dark"] if window_controls_enabled else self.palette["muted"],
            )
            self.screen_mode_button.configure(
                bg=self.palette["accent_light"] if not is_window_mode else "#ffffff",
                fg=self.palette["accent_dark"] if not is_window_mode else self.palette["text"],
            )
        self.pick_region_button.configure(text="슬라이드 영역 선택" if is_window_mode else "화면 영역 선택")
        self._update_selection_summary()
        self._refresh_selection_preview_from_state()

    def _format_window_item(self, item: Dict[str, Any], index: int) -> str:
        title = item.get("window_title") or "(제목 없음)"
        owner = item.get("window_owner") or "(앱 이름 없음)"
        return (
            f"{index:02d}. id={item['window_id']}  {owner}  "
            f"{item['width']}x{item['height']}  {title}"
        )

    def _refresh_windows(self) -> None:
        if self.source_mode_var.get() != "window":
            return
        if not self.window_mode_supported:
            self.window_candidates = []
            self.window_listbox.delete(0, "end")
            self.selection_summary_var.set("이 플랫폼에서는 창 고정 캡처를 사용할 수 없습니다. 화면 영역 직접 캡처를 사용하세요.")
            return
        try:
            owner_filter = self.window_owner_var.get().strip() or None
            title_filter = self.window_title_var.get().strip() or None
            candidates = self.sc.list_candidate_windows(owner_filter, title_filter)
            if not candidates and owner_filter == "Google Chrome":
                candidates = self.sc.list_candidate_windows("Chrome", title_filter)
        except Exception as exc:
            messagebox.showerror("창 목록 조회 실패", str(exc), parent=self.root)
            return

        self.window_candidates = candidates
        self.window_listbox.delete(0, "end")
        for idx, item in enumerate(candidates, start=1):
            self.window_listbox.insert("end", self._format_window_item(item, idx))

        if candidates:
            self.window_listbox.selection_clear(0, "end")
            self.window_listbox.selection_set(0)
            self.window_listbox.activate(0)
            self._on_window_selected()
        else:
            self.window_selection = None
            self.selection_summary_var.set("조건에 맞는 창을 찾지 못했습니다. Chrome 창을 앞으로 띄운 뒤 다시 시도해 주세요.")
            self.selection_image_label.configure(image="", text="표시할 창이 없습니다.")
            self.preview_photo = None

    def _on_window_selected(self, _event: Optional[tk.Event] = None) -> None:
        selection = self._current_window_candidate()
        if selection is None:
            return
        if self.window_selection and self.window_selection.window.get("window_id") != selection.get("window_id"):
            self.window_selection = None
        self._update_selection_summary()
        self._refresh_selection_preview_from_state()

    def _current_window_candidate(self) -> Optional[Dict[str, Any]]:
        if not self.window_candidates:
            return None
        selected = self.window_listbox.curselection()
        if not selected:
            return None
        index = int(selected[0])
        if index < 0 or index >= len(self.window_candidates):
            return None
        return self.window_candidates[index]

    def _update_selection_summary(self) -> None:
        if self.source_mode_var.get() == "window":
            window = self._current_window_candidate()
            if window is None:
                self.selection_summary_var.set("캡처할 Chrome 창을 선택해 주세요.")
                return
            title = window.get("window_title") or "(제목 없음)"
            summary = (
                f"대상 창: {window.get('window_owner', '')} / {title}\n"
                f"창 ID: {window['window_id']}  크기: {window['width']} x {window['height']}"
            )
            if self.window_selection and self.window_selection.window.get("window_id") == window.get("window_id"):
                x, y, w, h = self.window_selection.roi
                summary += f"\n선택한 슬라이드 영역: left={x}, top={y}, width={w}, height={h}"
            else:
                summary += "\n아직 슬라이드 영역을 선택하지 않았습니다."
            self.selection_summary_var.set(summary)
            return

        if self.screen_selection:
            region = self.screen_selection.region
            self.selection_summary_var.set(
                "화면 전체에서 ROI를 직접 지정합니다.\n"
                f"선택한 영역: left={region['left']}, top={region['top']}, "
                f"width={region['width']}, height={region['height']}"
            )
        else:
            self.selection_summary_var.set("화면 캡처 모드입니다. 캡처할 영역을 먼저 선택해 주세요.")

    def _choose_region(self) -> None:
        if self.capture_thread and self.capture_thread.is_alive():
            messagebox.showinfo("캡처 진행 중", "캡처가 진행 중일 때는 영역을 다시 고를 수 없습니다.", parent=self.root)
            return

        self.root.configure(cursor="watch")
        self.root.update()
        try:
            if self.source_mode_var.get() == "window":
                if not self.window_mode_supported:
                    messagebox.showinfo("창 캡처", "이 플랫폼에서는 창 고정 캡처를 사용할 수 없습니다.", parent=self.root)
                    return
                candidate = self._current_window_candidate()
                if candidate is None:
                    messagebox.showinfo("대상 창 선택", "먼저 캡처할 창을 선택해 주세요.", parent=self.root)
                    return
                source = self.sc.create_window_source(
                    window_id=int(candidate["window_id"]),
                    window_owner=str(candidate["window_owner"]),
                    window_title=str(candidate["window_title"]),
                    backend=self.window_backend_var.get(),
                    pause_on_cursor_in_roi=self.pause_on_cursor_var.get(),
                )
                try:
                    preview = source.selection_preview()
                finally:
                    source.close()
                dialog = RoiSelectorDialog(
                    self.root,
                    preview,
                    "창 내부 슬라이드 영역 선택",
                    "선택한 창 스냅샷에서 슬라이드 영역만 드래그해 선택하세요.",
                )
                roi = dialog.show()
                if roi is None:
                    return
                self.window_selection = WindowRegionSelection(
                    window=candidate,
                    roi=roi,
                    source_size=(int(preview.shape[1]), int(preview.shape[0])),
                    preview_bgr=preview,
                )
                self._display_selection_preview(preview, roi)
            else:
                preview, monitor = self.sc.grab_full_desktop()
                dialog = RoiSelectorDialog(
                    self.root,
                    preview,
                    "화면 영역 선택",
                    "강의 슬라이드가 보이는 화면 영역만 드래그해 선택하세요.",
                )
                roi = dialog.show()
                if roi is None:
                    return
                left, top, width, height = roi
                region = {
                    "left": int(monitor["left"] + left),
                    "top": int(monitor["top"] + top),
                    "width": int(width),
                    "height": int(height),
                }
                self.screen_selection = ScreenRegionSelection(region=region, roi=roi, preview_bgr=preview)
                self._display_selection_preview(preview, roi)
        except Exception as exc:
            traceback.print_exc()
            messagebox.showerror("영역 선택 실패", str(exc), parent=self.root)
        finally:
            self.root.configure(cursor="")
            self._update_selection_summary()

    def _display_selection_preview(self, image_bgr: Any, roi: Optional[Tuple[int, int, int, int]]) -> None:
        image = self.Image.fromarray(image_bgr[:, :, ::-1])
        if roi is not None:
            draw = self.ImageDraw.Draw(image)
            x, y, w, h = roi
            draw.rectangle((x, y, x + w, y + h), outline="#f97316", width=6)
        image.thumbnail((560, 320), self.Image.Resampling.LANCZOS)
        self.preview_photo = self.ImageTk.PhotoImage(image)
        self.selection_image_label.configure(image=self.preview_photo, text="")

    def _clear_selection_preview(self, text: str) -> None:
        self.preview_photo = None
        self.selection_image_label.configure(image="", text=text)

    def _refresh_selection_preview_from_state(self) -> None:
        if self.source_mode_var.get() == "window":
            candidate = self._current_window_candidate()
            if (
                candidate is not None
                and self.window_selection is not None
                and self.window_selection.window.get("window_id") == candidate.get("window_id")
            ):
                self._display_selection_preview(self.window_selection.preview_bgr, self.window_selection.roi)
                return
            self._clear_selection_preview("선택한 창의 슬라이드 영역을 아직 지정하지 않았습니다.")
            return

        if self.screen_selection is not None:
            self._display_selection_preview(self.screen_selection.preview_bgr, self.screen_selection.roi)
            return
        self._clear_selection_preview("화면 영역을 아직 지정하지 않았습니다.")

    def _display_last_saved_preview(self, path: Path) -> None:
        try:
            image = self.Image.open(path).convert("RGB")
        except Exception:
            return
        image.thumbnail((560, 320), self.Image.Resampling.LANCZOS)
        self.last_saved_photo = self.ImageTk.PhotoImage(image)
        self.saved_image_label.configure(image=self.last_saved_photo, text="")
        self.last_thumb_path = path

    def _validate_interval(self) -> float:
        try:
            value = float(self.interval_var.get().strip())
        except ValueError as exc:
            raise RuntimeError("샘플링 간격은 숫자로 입력해 주세요.") from exc
        if value < 0.10:
            raise RuntimeError("샘플링 간격은 0.10초 이상이어야 합니다.")
        return value

    def _build_output_dir(self) -> Path:
        base = normalize_output_base(self.output_base_var.get())
        save_output_base(base)
        base.mkdir(parents=True, exist_ok=True)

        stem = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = base / stem
        suffix = 2
        while candidate.exists():
            candidate = base / f"{stem}_{suffix:02d}"
            suffix += 1
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    def _create_capture_source(self, output_dir: Path) -> Any:
        if self.source_mode_var.get() == "window":
            if not self.window_mode_supported:
                raise RuntimeError("이 플랫폼에서는 창 고정 캡처를 사용할 수 없습니다. 화면 영역 직접 캡처를 사용하세요.")
            candidate = self._current_window_candidate()
            if candidate is None:
                raise RuntimeError("캡처할 창을 선택해 주세요.")
            if self.window_selection is None or self.window_selection.window.get("window_id") != candidate.get("window_id"):
                raise RuntimeError("선택한 창에 대해 슬라이드 영역을 먼저 지정해 주세요.")
            source = self.sc.create_window_source(
                window_id=int(candidate["window_id"]),
                window_owner=str(candidate["window_owner"]),
                window_title=str(candidate["window_title"]),
                backend=self.window_backend_var.get(),
                pause_on_cursor_in_roi=self.pause_on_cursor_var.get(),
            )
            x, y, w, h = self.window_selection.roi
            source_w, source_h = self.window_selection.source_size
            source.set_roi_from_pixels(x, y, w, h, source_w, source_h)
        else:
            if self.screen_selection is None:
                raise RuntimeError("캡처할 화면 영역을 먼저 선택해 주세요.")
            source = self.sc.ScreenRegionSource(
                self.screen_selection.region,
                pause_on_cursor_in_roi=self.pause_on_cursor_var.get(),
            )

        self.sc.save_capture_json(source.to_json(), output_dir)
        return source

    def _start_capture(self) -> None:
        if self.capture_thread and self.capture_thread.is_alive():
            return

        try:
            interval = self._validate_interval()
            output_dir = self._build_output_dir()
            capture_source = self._create_capture_source(output_dir)
        except Exception as exc:
            messagebox.showerror("캡처 시작 실패", str(exc), parent=self.root)
            return

        config = self.sc.Config(sample_interval=interval)
        self.engine = self.sc.SlideCaptureEngine(
            capture_source=capture_source,
            output_dir=output_dir,
            config=config,
            mode=self.mode_var.get(),
            show_preview=False,
            make_pdf=self.make_pdf_var.get(),
            skip_duplicate_slides=not self.keep_duplicates_var.get(),
            dedupe_pdf=not self.keep_duplicates_var.get(),
        )
        self.output_dir = output_dir
        self.run_error = None
        self.finish_handled = False
        self.last_saved_count = -1
        self.last_thumb_path = None
        self.capture_started_at = datetime.now()
        self.saved_count_var.set("0")
        self.duplicate_count_var.set("0")
        self.session_dir_var.set(str(output_dir))
        self.last_saved_var.set("-")
        self.elapsed_var.set("00:00")
        self.saved_image_label.configure(image="", text="캡처가 시작되면 최근 저장된 슬라이드를 여기에 보여줍니다.")
        self.last_saved_photo = None
        self.session_status_var.set("캡처 중")
        self._append_log(f"[gui] 세션 시작: {output_dir}")
        self.pause_button_text.set("일시정지")
        self.gallery_snapshot = None
        self.saved_strip_snapshot = None
        self._refresh_saved_strip(force=True)

        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="normal")
        self.finish_button.configure(state="normal")
        self.pick_region_button.configure(state="disabled")
        self.refresh_button.configure(state="disabled")

        self.capture_thread = threading.Thread(target=self._capture_worker, name="LectureSlideCaptureWorker", daemon=True)
        self.capture_thread.start()

    def _capture_worker(self) -> None:
        try:
            if self.engine is None:
                return
            self.engine.run()
        except Exception:
            self.run_error = traceback.format_exc()
            print(self.run_error, file=sys.stderr)

    def _open_session_folder(self) -> None:
        target = self.output_dir or normalize_output_base(self.output_base_var.get())
        open_path(target)

    def _handle_capture_finished(self) -> None:
        if self.finish_handled:
            return
        self.finish_handled = True
        self.start_button.configure(state="normal")
        self.pause_button.configure(state="disabled")
        self.finish_button.configure(state="disabled")
        self.pause_button_text.set("일시정지")
        self.pick_region_button.configure(state="normal")
        self.refresh_button.configure(state="normal" if self.source_mode_var.get() == "window" else "disabled")

        if self.run_error:
            self.session_status_var.set("오류로 종료")
            if not self.close_requested:
                show_long_message(
                    self.root,
                    "캡처 오류",
                    "캡처 도중 오류가 발생했습니다. 아래 로그를 확인해 주세요.",
                    self.run_error,
                )
        else:
            self.session_status_var.set("완료")
            saved = self.engine.capture_count if self.engine is not None else 0
            duplicates = self.engine.duplicate_skip_count if self.engine is not None else 0
            if not self.close_requested:
                messagebox.showinfo(
                    "캡처 완료",
                    f"저장된 슬라이드: {saved}장\n중복으로 건너뛴 슬라이드: {duplicates}장",
                    parent=self.root,
                )

        self.capture_started_at = None

        if self.close_requested:
            self._shutdown()

    def _tick(self) -> None:
        if self.is_shutdown:
            return
        self._drain_logs()

        if self.capture_started_at is not None:
            elapsed_seconds = max(0, int((datetime.now() - self.capture_started_at).total_seconds()))
            self.elapsed_var.set(f"{elapsed_seconds // 60:02d}:{elapsed_seconds % 60:02d}")

        if self.engine is not None:
            self.saved_count_var.set(str(self.engine.capture_count))
            self.duplicate_count_var.set(str(self.engine.duplicate_skip_count))
            if self.engine.saved_paths:
                latest = self.engine.saved_paths[-1]
                self.last_saved_var.set(latest.name)
                if latest != self.last_thumb_path:
                    self._display_last_saved_preview(latest)
            self._refresh_saved_strip()

        if self.gallery_window is not None:
            self._refresh_slides_gallery()

        if self.capture_thread and not self.capture_thread.is_alive():
            self._handle_capture_finished()

        self.root.after(180, self._tick)

    def _on_close(self) -> None:
        if self.capture_thread and self.capture_thread.is_alive():
            should_stop = messagebox.askyesno(
                "캡처 종료",
                "캡처가 진행 중입니다. 종료 요청을 보낸 뒤 창을 닫을까요?\n현재까지 저장된 슬라이드로 PDF 생성까지 진행됩니다.",
                parent=self.root,
            )
            if not should_stop:
                return
            self.close_requested = True
            self._finish_capture()
            self.root.after(180, self._tick)
            return
        self._shutdown()

    def _shutdown(self) -> None:
        if self.is_shutdown:
            return
        self.is_shutdown = True
        try:
            self.root.unbind_all("<MouseWheel>")
        except Exception:
            pass
        self._close_gallery()
        try:
            sys.stdout = self.original_stdout
            sys.stderr = self.original_stderr
        except Exception:
            pass
        try:
            self.log_file.close()
        except Exception:
            pass
        self.root.destroy()


def import_capture_module() -> Any:
    script_dir = str(RES_DIR)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    import slide_capture

    return slide_capture


def main() -> None:
    enable_windows_dpi_awareness()
    maybe_reexec_with_usable_python()
    print(f"[gui-start] executable={sys.executable}")
    root = tk.Tk()
    show_bootstrap_screen(root, "앱을 준비하고 있습니다.\n필수 모듈과 캡처 엔진을 확인하는 중입니다.")
    missing = discover_missing_modules()
    if missing:
        missing_text = ", ".join(missing)
        install_command = build_install_command()
        clipboard_ready = False
        try:
            root.clipboard_clear()
            root.clipboard_append(install_command)
            clipboard_ready = True
        except Exception:
            clipboard_ready = False

        print(f"[install-required] missing={missing_text}")
        print(f"[install-required] command={install_command}")
        show_install_screen(root, missing_text, install_command, clipboard_ready)
        root.mainloop()
        return

    print("[gui-start] importing slide_capture")
    try:
        capture_module = import_capture_module()
    except Exception:
        details = traceback.format_exc()
        print("[gui-start] import failed")
        print(details)
        show_error_screen(
            root,
            "초기화 실패",
            "캡처 모듈을 불러오지 못했습니다. 아래 내용을 확인해 주세요.",
            details,
        )
        root.mainloop()
        return

    print("[gui-start] slide_capture imported")
    clear_root_content(root)
    CaptureApp(root, capture_module)
    print("[gui-start] app ui initialized")
    root.mainloop()


if __name__ == "__main__":
    main()
