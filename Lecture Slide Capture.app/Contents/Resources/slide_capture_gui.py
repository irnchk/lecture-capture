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


def _ui_font_families() -> Dict[str, str]:
    """Pick clean, platform-native type families with graceful fallbacks."""
    if sys.platform == "darwin":
        return {"display": "SF Pro Display", "text": "SF Pro Text", "mono": "SF Mono"}
    if sys.platform == "win32":
        return {"display": "Segoe UI Semibold", "text": "Segoe UI", "mono": "Cascadia Mono"}
    return {"display": "Inter", "text": "Inter", "mono": "DejaVu Sans Mono"}


FONT = _ui_font_families()


def ui_font(role: str = "text", size: int = 12, weight: str = "normal") -> tuple:
    """Build a Tk font spec from the central type system (role + size + weight)."""
    family = FONT.get(role, FONT["text"])
    if weight == "bold":
        return (family, size, "bold")
    return (family, size)


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
DEFAULT_OUTPUT_BASE = Path.home() / "Documents" / "Lecture Slide Capture"
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
    raise RuntimeError("Could not find a python3 executable.")


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
    ttk.Button(button_row, text="Close", command=dialog.destroy).pack(side="right")

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
        font=ui_font("display", 18, "bold"),
        anchor="w",
        justify="left",
    ).pack(fill="x", anchor="w")

    tk.Label(
        outer,
        text=message,
        bg="#f5f1e8",
        fg="#4b5563",
        font=ui_font("text", 12),
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
        font=ui_font("display", 17, "bold"),
        anchor="w",
        justify="left",
    ).pack(fill="x", anchor="w")

    tk.Label(
        outer,
        text=message,
        bg="#f5f1e8",
        fg="#4b5563",
        font=ui_font("text", 12),
        anchor="w",
        justify="left",
        wraplength=760,
        pady=12,
    ).pack(fill="x", anchor="w")

    text = scrolledtext.ScrolledText(
        outer,
        wrap="word",
        height=18,
        font=ui_font("mono", 11),
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

    tk.Button(button_row, text="Copy Details", command=copy_details, padx=14, pady=6).pack(side="left")
    tk.Button(button_row, text="Close", command=root.destroy, padx=16, pady=6).pack(side="right")

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
echo "Required Python packages are not installed yet."
echo
echo "Missing modules:"
printf '%s\\n' "$MISSING_TEXT"
echo
echo "Run this command:"
echo
printf '%s\\n\\n' "$INSTALL_CMD"
if command -v pbcopy >/dev/null 2>&1; then
  printf '%s' "$INSTALL_CMD" | pbcopy
  echo "(The install command has been copied to the clipboard.)"
  echo
fi
read -r -p "Press Enter to close this window..." _
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
        text="Required Python packages are not installed yet.",
        bg="#f5f1e8",
        fg="#1f2937",
        font=ui_font("display", 15, "bold"),
        anchor="w",
        justify="left",
    ).pack(fill="x", anchor="w")

    tk.Label(
        outer,
        text=f"Missing modules: {missing_text}",
        bg="#f5f1e8",
        fg="#374151",
        font=ui_font("text", 12),
        anchor="w",
        justify="left",
        wraplength=720,
        pady=10,
    ).pack(fill="x", anchor="w")

    note_text = (
        "The app does not install packages automatically.\n"
        "Run the command below in Terminal, then reopen the app.\n"
        "You can also open a Terminal window with the command prepared."
    )
    if clipboard_ready:
        note_text += "\nThe install command has also been copied to the clipboard."

    tk.Label(
        outer,
        text=note_text,
        bg="#f5f1e8",
        fg="#4b5563",
        font=ui_font("text", 12),
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
        font=ui_font("mono", 11),
    )
    command_box.pack(fill="x", expand=False, pady=(14, 0))
    command_box.insert("1.0", install_command)
    command_box.tag_add("all", "1.0", "end")
    command_box.configure(state="disabled")

    footer = tk.Label(
        outer,
        text="1. Open Terminal  2. Run the command  3. Reopen the app after installation",
        bg="#f5f1e8",
        fg="#6b7280",
        font=ui_font("text", 11),
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
        text="Copy Command",
        command=copy_command,
        padx=14,
        pady=6,
    ).pack(side="left")
    tk.Button(
        button_row,
        text="Open in Terminal",
        command=lambda: open_install_command_in_terminal(install_command, missing_text),
        padx=14,
        pady=6,
    ).pack(side="left", padx=(8, 0))
    tk.Button(
        button_row,
        text="Close",
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
        self.status_var = tk.StringVar(value="Drag to select the slide region.")
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
        ttk.Button(button_row, text="Reset", command=self._reset).pack(side="left")
        ttk.Button(button_row, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(button_row, text="Confirm", command=self._confirm).pack(side="right", padx=(0, 8))

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
        self.status_var.set(f"Selecting: {int(round(width))} x {int(round(height))}")

    def _on_release(self, event: tk.Event) -> None:
        self._on_drag(event)

    def _reset(self) -> None:
        self.drag_start = None
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
            self.rect_id = None
        self.result = None
        self.status_var.set("Drag to select the slide region.")

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()

    def _confirm(self) -> None:
        if self.drag_start is None or self.rect_id is None:
            messagebox.showinfo("Region Selection", "Drag to select a capture region first.", parent=self.top)
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
            messagebox.showinfo("Region Selection", "Select a region with width and height greater than zero.", parent=self.top)
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
        self.gallery_header_var = tk.StringVar(value="No session is open yet.")
        self.pause_button_text = tk.StringVar(value="Pause")

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
        self.selection_summary_var = tk.StringVar(value="No capture region has been selected yet.")
        self.session_status_var = tk.StringVar(value="Idle")
        self.saved_count_var = tk.StringVar(value="0")
        self.duplicate_count_var = tk.StringVar(value="0")
        self.session_dir_var = tk.StringVar(value="-")
        self.last_saved_var = tk.StringVar(value="-")
        self.elapsed_var = tk.StringVar(value="00:00")

        self._configure_root()
        self._build_ui()
        self._sync_source_mode_ui()
        self._refresh_windows()
        self._append_log("Lecture Slide Capture GUI is ready")
        self.root.after(180, self._tick)

    def _configure_root(self) -> None:
        self.palette = {
            "bg": "#fbfaf7",
            "panel": "#fffefa",
            "panel_alt": "#f8f6f1",
            "border": "#ded8cd",
            "divider": "#ece6da",
            "text": "#1f2937",
            "muted": "#667085",
            "faint": "#98a1b0",
            "accent": "#d97706",
            "accent_hover": "#ea8a0c",
            "accent_light": "#fff7ed",
            "accent_dark": "#92400e",
            "track": "#f1ece1",
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

        body_font = ui_font("text", 12)
        muted = self.palette["muted"]

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=body_font, background=self.palette["bg"], foreground=self.palette["text"])
        style.configure("Root.TFrame", background=self.palette["bg"])
        style.configure("Panel.TFrame", background=self.palette["panel"])
        style.configure("Soft.TFrame", background=self.palette["panel_alt"])
        style.configure("Header.TLabel", font=ui_font("display", 26, "bold"), foreground=self.palette["text"], background=self.palette["bg"])
        style.configure("Eyebrow.TLabel", font=ui_font("text", 10, "bold"), foreground=self.palette["faint"], background=self.palette["bg"])
        style.configure("Subheader.TLabel", font=ui_font("text", 12), foreground=muted, background=self.palette["bg"])
        style.configure("PanelSubheader.TLabel", font=ui_font("text", 11), foreground=muted, background=self.palette["panel"])
        style.configure("Muted.TLabel", font=ui_font("text", 11), foreground=muted)
        style.configure(
            "Step.TLabelframe",
            background=self.palette["panel"],
            bordercolor=self.palette["border"],
            relief="solid",
        )
        style.configure(
            "Step.TLabelframe.Label",
            font=ui_font("display", 14, "bold"),
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
            font=ui_font("text", 12, "bold"),
            foreground=self.palette["text"],
            background=self.palette["bg"],
        )
        style.configure("TLabel", background=self.palette["panel"], foreground=self.palette["text"], font=body_font)
        style.configure("TRadiobutton", background=self.palette["panel"], foreground=self.palette["text"], font=body_font)
        style.configure("TCheckbutton", background=self.palette["panel"], foreground=self.palette["text"], font=body_font)
        style.map("TCheckbutton", foreground=[("disabled", self.palette["faint"])])
        style.configure(
            "TEntry",
            fieldbackground="#ffffff",
            bordercolor=self.palette["border"],
            lightcolor=self.palette["border"],
            darkcolor=self.palette["border"],
            borderwidth=1,
            padding=(8, 6),
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", self.palette["accent"])],
            lightcolor=[("focus", self.palette["accent"])],
            darkcolor=[("focus", self.palette["accent"])],
        )
        style.configure(
            "TCombobox",
            fieldbackground="#ffffff",
            bordercolor=self.palette["border"],
            borderwidth=1,
            padding=(8, 5),
            arrowsize=14,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#ffffff")],
            bordercolor=[("focus", self.palette["accent"])],
        )
        style.configure(
            "TButton",
            font=body_font,
            padding=(14, 8),
            background=self.palette["panel_alt"],
            foreground=self.palette["text"],
            bordercolor=self.palette["border"],
            focuscolor=self.palette["accent_light"],
            relief="flat",
        )
        style.map(
            "TButton",
            background=[("active", "#efe8db"), ("pressed", "#e7e0d2"), ("disabled", "#f3f0ea")],
            foreground=[("disabled", self.palette["faint"])],
        )
        style.configure("Ghost.TButton", padding=(12, 7))
        style.configure(
            "Accent.TButton",
            font=ui_font("text", 12, "bold"),
            foreground="#ffffff",
            background=self.palette["accent"],
            bordercolor=self.palette["accent"],
            padding=(18, 9),
            relief="flat",
        )
        style.map(
            "Accent.TButton",
            background=[("active", self.palette["accent_hover"]), ("pressed", self.palette["accent_dark"]), ("disabled", "#e7e2d8")],
            foreground=[("disabled", self.palette["faint"])],
        )
        style.configure("Danger.TButton", font=ui_font("text", 12, "bold"), foreground=self.palette["danger"], padding=(14, 8))
        style.map("Danger.TButton", foreground=[("disabled", self.palette["faint"])])

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, style="Root.TFrame")
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)

        app_header = ttk.Frame(shell, padding=(24, 20, 24, 10), style="Root.TFrame")
        app_header.grid(row=0, column=0, sticky="ew")
        app_header.columnconfigure(0, weight=1)
        ttk.Label(app_header, text="LECTURE SLIDE CAPTURE", style="Eyebrow.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(app_header, text="Slide Capture Studio", style="Header.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(
            app_header,
            text="Choose a lecture window, mark the slide region, capture changes, and generate a PDF from one screen.",
            style="Subheader.TLabel",
        ).grid(row=2, column=0, sticky="w", pady=(5, 0))

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
            card.rowconfigure(2, weight=1)

            header_row = tk.Frame(card, bg=self.palette["panel"])
            header_row.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 12))
            header_row.columnconfigure(1, weight=1)
            if step_number:
                badge = tk.Canvas(header_row, width=34, height=34, bg=self.palette["panel"], highlightthickness=0, bd=0)
                badge.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 10))
                fill = accent or self.palette["accent"]
                badge.create_oval(2, 2, 32, 32, fill=fill, outline=fill)
                badge.create_text(17, 17, text=step_number, fill="#ffffff", font=ui_font("display", 15, "bold"))
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
                font=ui_font("display", 15, "bold"),
                anchor="w",
            ).grid(row=0, column=title_col, sticky="w")
            if subtitle:
                tk.Label(
                    header_row,
                    text=subtitle,
                    bg=self.palette["panel"],
                    fg=self.palette["muted"],
                    font=ui_font("text", 11),
                    anchor="w",
                ).grid(row=1, column=title_col, sticky="w", pady=(3, 0))

            tk.Frame(card, bg=self.palette["divider"], height=1, bd=0).grid(
                row=1, column=0, sticky="ew", padx=16
            )

            body = ttk.Frame(card, padding=padding, style="Panel.TFrame")
            body.grid(row=2, column=0, sticky="nsew")
            body.columnconfigure(0, weight=1)
            return body

        source_group = make_card(
            setup_frame,
            "1  Source",
            "Choose the window or screen region that shows the lecture.",
            accent=self.palette["info"],
            step_number="1",
            row=0,
            column=0,
            sticky="ew",
        )
        source_group.columnconfigure(1, weight=1)

        segmented = tk.Frame(
            source_group,
            bg=self.palette["track"],
            highlightthickness=1,
            highlightbackground=self.palette["border"],
            bd=0,
        )
        segmented.grid(row=0, column=0, columnspan=2, sticky="ew")
        segmented.columnconfigure(0, weight=1, uniform="seg")
        segmented.columnconfigure(1, weight=1, uniform="seg")

        self.window_mode_button = tk.Radiobutton(
            segmented,
            text="Capture Chrome Window",
            variable=self.source_mode_var,
            value="window",
            command=self._sync_source_mode_ui,
            indicatoron=False,
            bg=self.palette["accent_light"],
            fg=self.palette["accent_dark"],
            activebackground=self.palette["accent_light"],
            selectcolor=self.palette["accent_light"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=12,
            pady=9,
            anchor="center",
            font=ui_font("text", 12, "bold"),
        )
        self.window_mode_button.grid(row=0, column=0, sticky="nsew", padx=(3, 2), pady=3)
        self.screen_mode_button = tk.Radiobutton(
            segmented,
            text="Capture Screen Region",
            variable=self.source_mode_var,
            value="screen",
            command=self._sync_source_mode_ui,
            indicatoron=False,
            bg=self.palette["track"],
            fg=self.palette["muted"],
            activebackground=self.palette["track"],
            selectcolor=self.palette["track"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=12,
            pady=9,
            anchor="center",
            font=ui_font("text", 12),
        )
        self.screen_mode_button.grid(row=0, column=1, sticky="nsew", padx=(2, 3), pady=3)

        ttk.Label(source_group, text="App Name Filter").grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.owner_entry = ttk.Entry(source_group, textvariable=self.window_owner_var)
        self.owner_entry.grid(row=1, column=1, sticky="ew", pady=(12, 0))
        ttk.Label(source_group, text="Window Title Filter").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.title_entry = ttk.Entry(source_group, textvariable=self.window_title_var)
        self.title_entry.grid(row=2, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(source_group, text="Window Capture Backend").grid(row=3, column=0, sticky="w", pady=(8, 0))
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
        self.refresh_button = ttk.Button(button_row, text="Refresh Window List", command=self._refresh_windows)
        self.refresh_button.pack(side="left")

        self.window_listbox = tk.Listbox(
            window_group,
            height=4,
            activestyle="dotbox",
            exportselection=False,
            font=ui_font("mono", 11),
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
            "2  Region",
            "Mark only the visible slide area as the ROI.",
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
        self.pick_region_button = ttk.Button(region_group, text="Select Slide Region", command=self._choose_region)
        self.pick_region_button.grid(row=1, column=0, sticky="ew", pady=(12, 0))

        capture_group = make_card(
            setup_frame,
            "3  Capture",
            "Confirm the output location and detection options, then start.",
            accent=self.palette["success"],
            step_number="3",
            row=2,
            column=0,
            sticky="ew",
            pady=(12, 0),
        )
        capture_group.columnconfigure(1, weight=1)
        capture_group.columnconfigure(3, weight=1)
        ttk.Label(capture_group, text="Base Output Folder").grid(row=0, column=0, sticky="w")
        self.output_entry = ttk.Entry(capture_group, textvariable=self.output_base_var)
        self.output_entry.grid(row=0, column=1, sticky="ew", padx=(10, 8))
        ttk.Button(capture_group, text="Browse", command=self._browse_output_dir).grid(row=0, column=2)
        ttk.Label(capture_group, text="Detection Mode").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Combobox(
            capture_group,
            textvariable=self.mode_var,
            values=("slide", "detailed"),
            state="readonly",
        ).grid(row=1, column=1, sticky="ew", padx=(10, 8), pady=(10, 0))
        ttk.Label(capture_group, text="Interval").grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Entry(capture_group, textvariable=self.interval_var, width=8).grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=(10, 0))
        ttk.Checkbutton(capture_group, text="Create PDF on Finish", variable=self.make_pdf_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Checkbutton(capture_group, text="Keep Duplicate Slides", variable=self.keep_duplicates_var).grid(
            row=2, column=2, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Checkbutton(capture_group, text="Pause when cursor is inside ROI", variable=self.pause_on_cursor_var).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )

        ttk.Label(preview_frame, text="Live Status", style="Subheader.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))

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
            card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 12, 0))
            tk.Frame(card, bg=color, height=3, bd=0).pack(fill="x")
            inner = tk.Frame(card, bg="#ffffff")
            inner.pack(fill="both", expand=True, padx=14, pady=(11, 13))
            tk.Label(
                inner,
                text=title,
                bg="#ffffff",
                fg=self.palette["muted"],
                font=ui_font("text", 10, "bold"),
            ).pack(anchor="w")
            tk.Label(
                inner,
                textvariable=variable,
                bg="#ffffff",
                fg=color,
                font=ui_font("display", 26, "bold"),
            ).pack(anchor="w", pady=(5, 2))
            tk.Label(
                inner,
                text=detail,
                bg="#ffffff",
                fg=self.palette["faint"],
                font=ui_font("text", 11),
            ).pack(anchor="w")

        metric_card(metrics_frame, 0, "Status", self.session_status_var, "Ready.", self.palette["info"])
        metric_card(metrics_frame, 1, "Saved Slides", self.saved_count_var, "This session", self.palette["success"])
        metric_card(metrics_frame, 2, "Duplicates Skipped", self.duplicate_count_var, "This session", self.palette["accent_dark"])
        metric_card(metrics_frame, 3, "Elapsed Time", self.elapsed_var, "mm:ss", self.palette["text"])

        preview_grid = ttk.Frame(preview_frame, style="Root.TFrame")
        preview_grid.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        preview_grid.columnconfigure(0, weight=1)
        preview_grid.columnconfigure(1, weight=1)
        preview_grid.rowconfigure(0, weight=1)

        selection_group = make_card(
            preview_grid,
            "Selected Region Preview",
            padding=(14, 10, 14, 14),
            row=0,
            column=0,
            sticky="nsew",
            padx=(0, 8),
        )
        selection_group.columnconfigure(0, weight=1)
        selection_group.rowconfigure(0, weight=1)
        self.selection_image_label = ttk.Label(selection_group, text="No preview yet.", anchor="center")
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
        self.saved_image_label = ttk.Label(saved_group, text="The latest saved slide will appear here after capture starts.", anchor="center")
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
            font=ui_font("mono", 11),
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
        self.open_folder_button = ttk.Button(footer, text="Open Session Folder", command=self._open_session_folder)
        self.open_folder_button.grid(row=1, column=0, sticky="w")
        ttk.Label(
            footer,
            text="Select a region before starting capture. While running, only pause and finish are available.",
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
        self.view_slides_button = ttk.Button(action_row, text="View All Captures", command=self._open_slides_gallery)
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
            self.pause_button_text.set("Pause")
            self.session_status_var.set("Capturing")
            self._append_log("[gui] Capture resumed.")
        else:
            self.engine.stopper.pause()
            self.pause_button_text.set("Resume")
            self.session_status_var.set("Paused")
            self._append_log("[gui] Capture paused.")

    def _finish_capture(self) -> None:
        if self.engine is None:
            return
        self.engine.stopper.request_stop()
        self.session_status_var.set("Stopping")
        self._append_log("[gui] Finish requested.")
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

        self.gallery_header_var.set(f"Session folder: {output_root}\nSaved slides: {len(paths)}")

        if not paths:
            ttk.Label(
                self.gallery_inner,
                text="No saved slides yet.\nStart capture or wait for slide changes.",
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
                ttk.Label(card, text="Could not load preview.", anchor="center").grid(
                    row=0, column=0, sticky="nsew", pady=(20, 20)
                )

            ttk.Label(
                card,
                text=f"{index + 1:02d}. {path.name}",
                justify="left",
                wraplength=260,
            ).grid(row=1, column=0, sticky="w", pady=(8, 0))
            ttk.Button(card, text="Open File", command=lambda p=path: self._open_image_path(p)).grid(
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
        self.gallery_window.title("All Captures")
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
        ttk.Label(header, text="Captured Slides", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.gallery_header_var, justify="left").grid(row=1, column=0, sticky="w", pady=(6, 0))
        header_buttons = ttk.Frame(header, style="Root.TFrame")
        header_buttons.grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Button(header_buttons, text="Refresh", command=lambda: self._refresh_slides_gallery(force=True)).pack(side="left")
        ttk.Button(header_buttons, text="Open Session Folder", command=self._open_session_folder).pack(side="left", padx=(8, 0))

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
                text="No saved slides yet.",
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
                ttk.Label(cell, text="No preview", anchor="center").grid(row=0, column=0, padx=6, pady=(16, 12))
            ttk.Label(
                cell,
                text=path.name[:18] + ("..." if len(path.name) > 18 else ""),
                anchor="center",
                style="PanelSubheader.TLabel",
            ).grid(row=1, column=0, sticky="ew", padx=6, pady=(4, 6))

    def _log_tag_for_line(self, line: str) -> str:
        lowered = line.lower()
        if "[saved]" in lowered or "saved" in lowered:
            return "saved"
        if "[wait]" in lowered or "[window-guard]" in lowered:
            return "wait"
        if "error" in lowered or "traceback" in lowered:
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
            title="Choose Base Output Folder",
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
            active_bg = self.palette["accent_light"]
            active_fg = self.palette["accent_dark"]
            idle_bg = self.palette["track"]
            idle_fg = self.palette["muted"]
            self.window_mode_button.configure(state="normal" if self.window_mode_supported else "disabled")
            window_active = window_controls_enabled
            self.window_mode_button.configure(
                bg=active_bg if window_active else idle_bg,
                fg=active_fg if window_active else idle_fg,
                activebackground=active_bg if window_active else idle_bg,
                selectcolor=active_bg if window_active else idle_bg,
                font=ui_font("text", 12, "bold") if window_active else ui_font("text", 12),
            )
            screen_active = not is_window_mode
            self.screen_mode_button.configure(
                bg=active_bg if screen_active else idle_bg,
                fg=active_fg if screen_active else idle_fg,
                activebackground=active_bg if screen_active else idle_bg,
                selectcolor=active_bg if screen_active else idle_bg,
                font=ui_font("text", 12, "bold") if screen_active else ui_font("text", 12),
            )
        self.pick_region_button.configure(text="Select Slide Region" if is_window_mode else "Select Screen Region")
        self._update_selection_summary()
        self._refresh_selection_preview_from_state()

    def _format_window_item(self, item: Dict[str, Any], index: int) -> str:
        title = item.get("window_title") or "(Untitled)"
        owner = item.get("window_owner") or "(Unknown App)"
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
            self.selection_summary_var.set("Window capture is not available on this platform. Use screen-region capture instead.")
            return
        try:
            owner_filter = self.window_owner_var.get().strip() or None
            title_filter = self.window_title_var.get().strip() or None
            candidates = self.sc.list_candidate_windows(owner_filter, title_filter)
            if not candidates and owner_filter == "Google Chrome":
                candidates = self.sc.list_candidate_windows("Chrome", title_filter)
        except Exception as exc:
            messagebox.showerror("Window List Failed", str(exc), parent=self.root)
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
            self.selection_summary_var.set("No matching window found. Bring the Chrome lecture window forward and try again.")
            self.selection_image_label.configure(image="", text="No window to display.")
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
                self.selection_summary_var.set("Select the Chrome window to capture.")
                return
            title = window.get("window_title") or "(Untitled)"
            summary = (
                f"Target window: {window.get('window_owner', '')} / {title}\n"
                f"Window ID: {window['window_id']}  Size: {window['width']} x {window['height']}"
            )
            if self.window_selection and self.window_selection.window.get("window_id") == window.get("window_id"):
                x, y, w, h = self.window_selection.roi
                summary += f"\nSelected slide region: left={x}, top={y}, width={w}, height={h}"
            else:
                summary += "\nNo slide region has been selected yet."
            self.selection_summary_var.set(summary)
            return

        if self.screen_selection:
            region = self.screen_selection.region
            self.selection_summary_var.set(
                "Select the ROI directly from the full screen.\n"
                f"Selected region: left={region['left']}, top={region['top']}, "
                f"width={region['width']}, height={region['height']}"
            )
        else:
            self.selection_summary_var.set("Screen capture mode. Select the capture region first.")

    def _choose_region(self) -> None:
        if self.capture_thread and self.capture_thread.is_alive():
            messagebox.showinfo("Capture Running", "You cannot choose a new region while capture is running.", parent=self.root)
            return

        self.root.configure(cursor="watch")
        self.root.update()
        try:
            if self.source_mode_var.get() == "window":
                if not self.window_mode_supported:
                    messagebox.showinfo("Window Capture", "Window capture is not available on this platform.", parent=self.root)
                    return
                candidate = self._current_window_candidate()
                if candidate is None:
                    messagebox.showinfo("Select Target Window", "Select a window to capture first.", parent=self.root)
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
                    "Select Slide Region Inside Window",
                    "Drag over the slide area in the selected window snapshot.",
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
                    "Select Screen Region",
                    "Drag over the screen area where the lecture slide is visible.",
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
            messagebox.showerror("Region Selection Failed", str(exc), parent=self.root)
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
            self._clear_selection_preview("No slide region has been selected for the chosen window yet.")
            return

        if self.screen_selection is not None:
            self._display_selection_preview(self.screen_selection.preview_bgr, self.screen_selection.roi)
            return
        self._clear_selection_preview("No screen region has been selected yet.")

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
            raise RuntimeError("Enter a numeric sampling interval.") from exc
        if value < 0.10:
            raise RuntimeError("The sampling interval must be at least 0.10 seconds.")
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
                raise RuntimeError("Window capture is not available on this platform. Use screen-region capture instead.")
            candidate = self._current_window_candidate()
            if candidate is None:
                raise RuntimeError("Select a window to capture.")
            if self.window_selection is None or self.window_selection.window.get("window_id") != candidate.get("window_id"):
                raise RuntimeError("Select a slide region for the chosen window first.")
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
                raise RuntimeError("Select a screen region to capture first.")
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
            messagebox.showerror("Capture Start Failed", str(exc), parent=self.root)
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
        self.saved_image_label.configure(image="", text="The latest saved slide will appear here after capture starts.")
        self.last_saved_photo = None
        self.session_status_var.set("Capturing")
        self._append_log(f"[gui] Session started: {output_dir}")
        self.pause_button_text.set("Pause")
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
        self.pause_button_text.set("Pause")
        self.pick_region_button.configure(state="normal")
        self.refresh_button.configure(state="normal" if self.source_mode_var.get() == "window" else "disabled")

        if self.run_error:
            self.session_status_var.set("Ended with Error")
            if not self.close_requested:
                show_long_message(
                    self.root,
                    "Capture Error",
                    "An error occurred during capture. Check the log below.",
                    self.run_error,
                )
        else:
            self.session_status_var.set("Complete")
            saved = self.engine.capture_count if self.engine is not None else 0
            duplicates = self.engine.duplicate_skip_count if self.engine is not None else 0
            if not self.close_requested:
                messagebox.showinfo(
                    "Capture Complete",
                    f"Saved slides: {saved}\nDuplicate slides skipped: {duplicates}",
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
                "Finish Capture",
                "Capture is still running. Send a finish request and close the window?\nA PDF will be generated from the slides saved so far.",
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
    show_bootstrap_screen(root, "Preparing the app.\nChecking required modules and the capture engine.")
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
            "Initialization Failed",
            "Could not load the capture module. Check the details below.",
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
