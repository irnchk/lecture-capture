from __future__ import annotations

import argparse
import csv
import json
import re
import signal
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from mss import mss
from skimage.metrics import structural_similarity as structural_similarity

try:
    from PIL import Image
except Exception:  # Pillow is optional unless img2pdf fallback is needed.
    Image = None

try:
    import img2pdf
except Exception:  # img2pdf is optional.
    img2pdf = None

if sys.platform == "darwin":
    try:
        import Quartz
    except Exception:
        Quartz = None

    try:
        import AppKit
        from Foundation import NSDate, NSRunLoop
    except Exception:
        AppKit = None
        NSDate = None
        NSRunLoop = None

    try:
        import ScreenCaptureKit
    except Exception:
        ScreenCaptureKit = None
else:  # pragma: no cover - platform guard
    Quartz = None
    AppKit = None
    NSDate = None
    NSRunLoop = None
    ScreenCaptureKit = None


PREVIEW_WINDOW_NAME = "Slide Capture Preview"


@dataclass
class TriggerThreshold:
    area: float
    block_ratio: float
    ssim: float


@dataclass
class Config:
    sample_interval: float = 0.60
    preprocess_width: int = 320
    diff_threshold: int = 18
    block_rows: int = 9
    block_cols: int = 16

    # New scene must remain nearly unchanged for a short period.
    stable_inter_area: float = 0.008
    stable_inter_ssim: float = 0.992
    settle_frames: int = 2
    min_candidate_seconds: float = 0.80
    max_candidate_seconds: float = 8.00

    # Pick the earliest frame that already matches the final stable scene.
    final_match_area: float = 0.006
    final_match_ssim: float = 0.993

    # Avoid duplicate captures immediately after a save.
    min_save_gap_seconds: float = 1.80

    # Repeatedly returning to the exact same slide should not create duplicate pages.
    duplicate_area_max: float = 0.006
    duplicate_block_ratio_max: float = 0.020
    duplicate_ssim_min: float = 0.995
    duplicate_hash_distance_max: int = 10

    # Local abrupt changes right after a save are usually pen / cursor / controls.
    abrupt_area: float = 0.015
    abrupt_block_ratio: float = 0.070
    abrupt_ssim: float = 0.985

    slide_mode: TriggerThreshold = field(
        default_factory=lambda: TriggerThreshold(area=0.060, block_ratio=0.220, ssim=0.900)
    )
    detailed_mode: TriggerThreshold = field(
        default_factory=lambda: TriggerThreshold(area=0.020, block_ratio=0.100, ssim=0.965)
    )

    # When cooldown is active, only wide/global changes may start a candidate.
    cooldown_wide_multiplier: float = 1.0


@dataclass
class FrameRep:
    small_bgr: np.ndarray
    blur_gray: np.ndarray


@dataclass
class Metrics:
    ssim: float
    area: float
    block_ratio: float


@dataclass
class HistoryItem:
    timestamp: float
    frame_bgr: np.ndarray
    rep: FrameRep


@dataclass
class Candidate:
    started_at: float
    history: List[HistoryItem] = field(default_factory=list)
    stable_count: int = 0


@dataclass
class SavedSlideRecord:
    index: int
    path: Path
    rep: FrameRep
    saved_at: float
    frame_hash: int


@dataclass
class DuplicateMatch:
    record: SavedSlideRecord
    metrics: Metrics
    hash_distance: int


@dataclass
class SourceDescriptor:
    source_type: str
    backend: str
    region_left: int
    region_top: int
    region_width: int
    region_height: int
    region_units: str
    window_id: Optional[int] = None
    window_owner: str = ""
    window_title: str = ""


@dataclass
class WindowSnapshot:
    left: int
    top: int
    width: int
    height: int
    alpha: float = 1.0
    is_onscreen: bool = True
    layer: int = 0


@dataclass
class FrameHealth:
    mean_luma: float
    std_luma: float
    dark_ratio: float
    edge_ratio: float


class CaptureUnavailableError(RuntimeError):
    """Raised when the source exists conceptually but cannot provide a frame right now."""


def current_mouse_global_location() -> Optional[Tuple[float, float]]:
    if Quartz is None:
        return None

    try:
        event = Quartz.CGEventCreate(None)
        if event is None:
            return None
        point = Quartz.CGEventGetLocation(event)
        return float(point.x), float(point.y)
    except Exception:
        return None


def point_in_rect(
    x: float,
    y: float,
    *,
    left: float,
    top: float,
    width: float,
    height: float,
    margin: float = 0.0,
) -> bool:
    return (
        x >= (left - margin)
        and x < (left + width + margin)
        and y >= (top - margin)
        and y < (top + height + margin)
    )


def mouse_inside_region(region: Dict[str, int], margin: float = 0.0) -> bool:
    location = current_mouse_global_location()
    if location is None:
        return False
    x, y = location
    return point_in_rect(
        x,
        y,
        left=float(region["left"]),
        top=float(region["top"]),
        width=float(region["width"]),
        height=float(region["height"]),
        margin=margin,
    )


class GracefulStop:
    def __init__(self) -> None:
        self.stop = False
        self.paused = False
        signal.signal(signal.SIGINT, self._handle)
        try:
            signal.signal(signal.SIGTERM, self._handle)
        except Exception:
            pass

    def _handle(self, signum, frame) -> None:  # pragma: no cover - signal handler
        self.stop = True
        self.paused = False

    def request_stop(self) -> None:
        self.stop = True
        self.paused = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False


class CaptureSource(ABC):
    @abstractmethod
    def capture_frame(self) -> np.ndarray:
        raise NotImplementedError

    @abstractmethod
    def selection_preview(self) -> np.ndarray:
        raise NotImplementedError

    @abstractmethod
    def descriptor(self) -> SourceDescriptor:
        raise NotImplementedError

    @abstractmethod
    def to_json(self) -> Dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class ScreenRegionSource(CaptureSource):
    def __init__(self, capture_region: Dict[str, int], pause_on_cursor_in_roi: bool = True) -> None:
        self.capture_region = capture_region.copy()
        self.pause_on_cursor_in_roi = bool(pause_on_cursor_in_roi)
        self.cursor_pause_margin_pixels = 8.0
        self._sct = mss()

    def capture_frame(self) -> np.ndarray:
        if self.pause_on_cursor_in_roi and mouse_inside_region(
            self.capture_region,
            margin=self.cursor_pause_margin_pixels,
        ):
            raise CaptureUnavailableError("커서가 ROI 안에 있어 캡처를 잠시 멈춥니다.")

        shot = self._sct.grab(self.capture_region)
        img = np.array(shot)
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    def selection_preview(self) -> np.ndarray:
        return self.capture_frame()

    def descriptor(self) -> SourceDescriptor:
        return SourceDescriptor(
            source_type="screen-region",
            backend="mss",
            region_left=int(self.capture_region["left"]),
            region_top=int(self.capture_region["top"]),
            region_width=int(self.capture_region["width"]),
            region_height=int(self.capture_region["height"]),
            region_units="screen_px",
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "source_type": "screen-region",
            "backend": "mss",
            "screen_region": self.capture_region,
        }

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass


class MacWindowSource(CaptureSource):
    def __init__(
        self,
        window_id: int,
        window_owner: str,
        window_title: str,
        backend: str = "auto",
        pause_on_cursor_in_roi: bool = True,
    ) -> None:
        if Quartz is None or AppKit is None:
            raise RuntimeError(
                "macOS 창 캡처에는 pyobjc-framework-Quartz 와 pyobjc-framework-Cocoa 가 필요합니다."
            )

        self.window_id = int(window_id)
        self.window_owner = window_owner
        self.window_title = window_title
        self.backend = self._resolve_backend(backend)
        self.selection_roi_px: Optional[Dict[str, int]] = None
        self.selection_source_size: Optional[Tuple[int, int]] = None
        self.norm_roi: Optional[Tuple[float, float, float, float]] = None
        self._sc_window: Any = None
        self._logged_backend_fallback = False
        self.anchor_window_snapshot: Optional[WindowSnapshot] = None
        self.anchor_capture_size: Optional[Tuple[int, int]] = None
        self.pause_on_cursor_in_roi = bool(pause_on_cursor_in_roi)
        self.cursor_pause_margin_pixels = 8.0
        self.window_guard_move_pixels = 80
        self.window_guard_resize_ratio = 0.05
        self.capture_guard_resize_ratio = 0.08
        self.unstable_settle_seconds = 1.20
        self.black_frame_mean_max = 10.0
        self.black_frame_dark_ratio = 0.992
        self.black_frame_std_max = 8.0
        self.black_frame_edge_ratio = 0.0018
        self.uniform_frame_std_max = 1.2
        self.uniform_frame_edge_ratio = 0.0004
        self._unstable_since: Optional[float] = None
        self._last_guard_log_monotonic = -10_000.0
        self._last_valid_frame_health: Optional[FrameHealth] = None

    def _resolve_backend(self, requested: str) -> str:
        requested = requested.lower()
        if requested not in {"auto", "screencapturekit", "coregraphics"}:
            raise RuntimeError(f"지원하지 않는 창 캡처 백엔드입니다: {requested}")

        if requested in {"auto", "screencapturekit"} and self._screen_capture_kit_available():
            return "screencapturekit"
        if requested in {"auto", "coregraphics"} and Quartz is not None:
            return "coregraphics"

        raise RuntimeError(
            "사용 가능한 macOS 창 캡처 백엔드를 찾지 못했습니다. "
            "ScreenCaptureKit 또는 Quartz(PyObjC) 설치 상태를 확인하세요."
        )

    @staticmethod
    def _screen_capture_kit_available() -> bool:
        return (
            ScreenCaptureKit is not None
            and hasattr(ScreenCaptureKit, "SCShareableContent")
            and hasattr(ScreenCaptureKit, "SCScreenshotManager")
            and hasattr(
                ScreenCaptureKit.SCScreenshotManager,
                "captureImageWithFilter_configuration_completionHandler_",
            )
        )

    def set_roi_from_pixels(
        self,
        left: int,
        top: int,
        width: int,
        height: int,
        source_width: int,
        source_height: int,
    ) -> None:
        if width <= 0 or height <= 0:
            raise RuntimeError("선택한 ROI 가 비어 있습니다.")
        if source_width <= 0 or source_height <= 0:
            raise RuntimeError("창 미리보기 크기가 올바르지 않습니다.")

        self.selection_roi_px = {
            "left": int(left),
            "top": int(top),
            "width": int(width),
            "height": int(height),
        }
        self.selection_source_size = (int(source_width), int(source_height))
        self.norm_roi = (
            float(left) / float(source_width),
            float(top) / float(source_height),
            float(width) / float(source_width),
            float(height) / float(source_height),
        )

    def selection_preview(self) -> np.ndarray:
        preview = self._capture_full_window(require_stable=False)
        self.anchor_capture_size = (int(preview.shape[1]), int(preview.shape[0]))
        snapshot = self._query_window_snapshot()
        if snapshot is not None:
            self.anchor_window_snapshot = snapshot
        return preview

    def capture_frame(self) -> np.ndarray:
        full_window = self._capture_full_window(require_stable=True)
        return self._crop_full_window(full_window)

    def descriptor(self) -> SourceDescriptor:
        if self.selection_roi_px is None:
            raise RuntimeError("창 내부 ROI 가 아직 설정되지 않았습니다.")

        return SourceDescriptor(
            source_type="mac-window",
            backend=self.backend,
            region_left=int(self.selection_roi_px["left"]),
            region_top=int(self.selection_roi_px["top"]),
            region_width=int(self.selection_roi_px["width"]),
            region_height=int(self.selection_roi_px["height"]),
            region_units="window_px_at_selection",
            window_id=self.window_id,
            window_owner=self.window_owner,
            window_title=self.window_title,
        )

    def to_json(self) -> Dict[str, Any]:
        if self.selection_roi_px is None or self.selection_source_size is None or self.norm_roi is None:
            raise RuntimeError("창 내부 ROI 가 아직 설정되지 않았습니다.")

        return {
            "source_type": "mac-window",
            "backend": self.backend,
            "window": {
                "window_id": self.window_id,
                "window_owner": self.window_owner,
                "window_title": self.window_title,
            },
            "selection_source_size": {
                "width": self.selection_source_size[0],
                "height": self.selection_source_size[1],
            },
            "roi_pixels": self.selection_roi_px,
            "roi_normalized": {
                "x": self.norm_roi[0],
                "y": self.norm_roi[1],
                "width": self.norm_roi[2],
                "height": self.norm_roi[3],
            },
        }

    def _cursor_inside_roi(self) -> bool:
        if not self.pause_on_cursor_in_roi or self.norm_roi is None:
            return False

        location = current_mouse_global_location()
        if location is None:
            return False

        snapshot = self._query_window_snapshot()
        if snapshot is None or not snapshot.is_onscreen or snapshot.width <= 0 or snapshot.height <= 0:
            return False

        x_frac, y_frac, w_frac, h_frac = self.norm_roi
        roi_left = snapshot.left + (snapshot.width * x_frac)
        roi_top = snapshot.top + (snapshot.height * y_frac)
        roi_width = snapshot.width * w_frac
        roi_height = snapshot.height * h_frac
        mouse_x, mouse_y = location
        return point_in_rect(
            mouse_x,
            mouse_y,
            left=roi_left,
            top=roi_top,
            width=roi_width,
            height=roi_height,
            margin=self.cursor_pause_margin_pixels,
        )

    def _capture_full_window(self, require_stable: bool = True) -> np.ndarray:
        if require_stable and self._cursor_inside_roi():
            raise CaptureUnavailableError("커서가 ROI 안에 있어 캡처를 잠시 멈춥니다.")

        if self.backend == "screencapturekit":
            first_error: Optional[Exception] = None
            for _ in range(2):
                try:
                    full_window = self._capture_with_screencapturekit()
                    break
                except CaptureUnavailableError as exc:
                    first_error = exc
                    self._sc_window = None
            else:
                if not require_stable and Quartz is not None:
                    if not self._logged_backend_fallback:
                        print(f"[window] ScreenCaptureKit 미리보기 실패, CoreGraphics 로 일시 폴백합니다: {first_error}")
                        self._logged_backend_fallback = True
                    full_window = self._capture_with_coregraphics()
                elif first_error is not None:
                    raise first_error
                else:
                    raise CaptureUnavailableError("ScreenCaptureKit 창 캡처에 실패했습니다.")
        else:
            full_window = self._capture_with_coregraphics()

        if require_stable:
            self._guard_window_transition(full_window)
        return full_window

    def _query_window_snapshot(self) -> Optional[WindowSnapshot]:
        if Quartz is None:
            return None

        info_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionIncludingWindow,
            self.window_id,
        )
        if info_list is None:
            return None

        for info in list(info_list):
            try:
                window_id = int(dict_get(info, Quartz.kCGWindowNumber, "kCGWindowNumber", default=0) or 0)
            except Exception:
                continue
            if window_id != self.window_id:
                continue

            bounds = dict_get(info, Quartz.kCGWindowBounds, "kCGWindowBounds", default={}) or {}
            try:
                left = int(bounds.get("X", 0))
                top = int(bounds.get("Y", 0))
                width = int(bounds.get("Width", 0))
                height = int(bounds.get("Height", 0))
            except Exception:
                continue

            return WindowSnapshot(
                left=left,
                top=top,
                width=width,
                height=height,
                alpha=float(dict_get(info, Quartz.kCGWindowAlpha, "kCGWindowAlpha", default=1.0) or 1.0),
                is_onscreen=bool(dict_get(info, Quartz.kCGWindowIsOnscreen, "kCGWindowIsOnscreen", default=True)),
                layer=int(dict_get(info, Quartz.kCGWindowLayer, "kCGWindowLayer", default=0) or 0),
            )
        return None

    def _compute_frame_health(self, full_window: np.ndarray) -> FrameHealth:
        h, w = full_window.shape[:2]
        target_w = min(320, w)
        target_h = max(1, int(round(h * target_w / max(1, w))))
        if target_w != w:
            small = cv2.resize(full_window, (target_w, target_h), interpolation=cv2.INTER_AREA)
        else:
            small = full_window

        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        mean_luma = float(gray.mean())
        std_luma = float(gray.std())
        dark_ratio = float(np.mean(gray <= 12))
        edges = cv2.Canny(gray, 40, 120)
        edge_ratio = float(np.mean(edges > 0))
        return FrameHealth(
            mean_luma=mean_luma,
            std_luma=std_luma,
            dark_ratio=dark_ratio,
            edge_ratio=edge_ratio,
        )

    def _frame_health_reasons(self, health: FrameHealth) -> List[str]:
        reasons: List[str] = []
        if (
            health.mean_luma <= self.black_frame_mean_max
            and health.dark_ratio >= self.black_frame_dark_ratio
            and health.std_luma <= self.black_frame_std_max
            and health.edge_ratio <= self.black_frame_edge_ratio
        ):
            reasons.append(
                f"black-frame mean={health.mean_luma:.1f} dark={health.dark_ratio:.3f} "
                f"std={health.std_luma:.1f} edges={health.edge_ratio:.4f}"
            )

        previous_health = self._last_valid_frame_health
        if (
            previous_health is not None
            and previous_health.mean_luma >= 18.0
            and health.mean_luma <= self.black_frame_mean_max
            and health.dark_ratio >= self.black_frame_dark_ratio
        ):
            reasons.append(
                f"sudden-blackout prev-mean={previous_health.mean_luma:.1f} "
                f"curr-mean={health.mean_luma:.1f}"
            )

        if health.std_luma <= self.uniform_frame_std_max and health.edge_ratio <= self.uniform_frame_edge_ratio:
            reasons.append(
                f"uniform-frame mean={health.mean_luma:.1f} std={health.std_luma:.2f} "
                f"edges={health.edge_ratio:.4f}"
            )
        return reasons

    def _refresh_sc_window_state_reasons(self) -> List[str]:
        reasons: List[str] = []
        if self.backend != "screencapturekit" or not self._screen_capture_kit_available():
            return reasons

        try:
            sc_window = self._get_sc_window(force_refresh=True)
        except Exception as exc:
            return [f"sc-window-refresh-failed={exc}"]

        if sc_window is None:
            return ["sc-window-missing"]

        try:
            if not bool(get_objc_property(sc_window, "isOnScreen")):
                reasons.append("sc-window-offscreen")
        except Exception:
            pass

        for prop_name in ("isMinimized", "isMiniaturized"):
            try:
                if bool(get_objc_property(sc_window, prop_name)):
                    reasons.append("sc-window-minimized")
                    break
            except Exception:
                continue
        return reasons

    def _guard_window_transition(self, full_window: np.ndarray) -> None:
        now = time.monotonic()
        reasons: List[str] = []
        snapshot = self._query_window_snapshot()
        frame_health = self._compute_frame_health(full_window)

        if self.anchor_capture_size is None:
            self.anchor_capture_size = (int(full_window.shape[1]), int(full_window.shape[0]))
        if self.anchor_window_snapshot is None and snapshot is not None:
            self.anchor_window_snapshot = snapshot

        if snapshot is None:
            reasons.append("window-info-missing")
        else:
            if snapshot.layer != 0:
                reasons.append(f"layer={snapshot.layer}")
            if not snapshot.is_onscreen:
                reasons.append("offscreen")
            if snapshot.alpha <= 0.01:
                reasons.append(f"alpha={snapshot.alpha:.2f}")

            anchor = self.anchor_window_snapshot
            if anchor is not None:
                allowed_w = max(40, int(round(anchor.width * self.window_guard_resize_ratio)))
                allowed_h = max(40, int(round(anchor.height * self.window_guard_resize_ratio)))
                if abs(snapshot.width - anchor.width) > allowed_w or abs(snapshot.height - anchor.height) > allowed_h:
                    reasons.append(
                        f"window-size {snapshot.width}x{snapshot.height} vs {anchor.width}x{anchor.height}"
                    )

                move_dx = abs(snapshot.left - anchor.left)
                move_dy = abs(snapshot.top - anchor.top)
                if move_dx > self.window_guard_move_pixels or move_dy > self.window_guard_move_pixels:
                    reasons.append(f"window-move dx={move_dx} dy={move_dy}")

        anchor_capture = self.anchor_capture_size
        if anchor_capture is not None:
            capture_w = int(full_window.shape[1])
            capture_h = int(full_window.shape[0])
            allowed_capture_w = max(32, int(round(anchor_capture[0] * self.capture_guard_resize_ratio)))
            allowed_capture_h = max(32, int(round(anchor_capture[1] * self.capture_guard_resize_ratio)))
            if abs(capture_w - anchor_capture[0]) > allowed_capture_w or abs(capture_h - anchor_capture[1]) > allowed_capture_h:
                reasons.append(
                    f"capture-size {capture_w}x{capture_h} vs {anchor_capture[0]}x{anchor_capture[1]}"
                )

        health_reasons = self._frame_health_reasons(frame_health)
        if health_reasons:
            reasons.extend(health_reasons)
            reasons.extend(self._refresh_sc_window_state_reasons())
            self._sc_window = None

        if reasons:
            if self._unstable_since is None:
                self._unstable_since = now
            if now - self._last_guard_log_monotonic >= 2.0:
                print(f"[window-guard] 전환 감지로 프레임을 건너뜁니다: {', '.join(reasons)}")
                self._last_guard_log_monotonic = now
            raise CaptureUnavailableError("Mission Control/데스크탑 전환/창 이동 중이라 프레임을 잠시 건너뜁니다.")

        if self._unstable_since is not None:
            if now - self._unstable_since < self.unstable_settle_seconds:
                raise CaptureUnavailableError("창 전환 직후 안정화 중입니다.")
            self._unstable_since = None

        self._last_valid_frame_health = frame_health

    def _capture_with_coregraphics(self) -> np.ndarray:
        if Quartz is None:
            raise CaptureUnavailableError("Quartz 가 없어 CoreGraphics 창 캡처를 사용할 수 없습니다.")

        image_options = Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageBestResolution
        image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull,
            Quartz.kCGWindowListOptionIncludingWindow,
            self.window_id,
            image_options,
        )
        if image is None:
            raise CaptureUnavailableError(
                "CoreGraphics 로 Chrome 창을 가져오지 못했습니다. 창이 닫혔거나 화면 기록 권한이 없을 수 있습니다."
            )
        return cgimage_to_bgr(image)

    def _capture_with_screencapturekit(self) -> np.ndarray:
        if not self._screen_capture_kit_available():
            raise CaptureUnavailableError("ScreenCaptureKit 스크린샷 API 를 사용할 수 없습니다.")

        sc_window = self._get_sc_window()
        if sc_window is None:
            raise CaptureUnavailableError(
                "ScreenCaptureKit 에서 지정한 창을 찾지 못했습니다. 창이 닫혔거나 화면 기록 권한이 없을 수 있습니다."
            )

        content_filter = ScreenCaptureKit.SCContentFilter.alloc().initWithDesktopIndependentWindow_(sc_window)
        content_rect: Any = None
        try:
            content_rect = get_objc_property(content_filter, "contentRect")
            point_pixel_scale = float(get_objc_property(content_filter, "pointPixelScale"))
            content_w, content_h = cgrect_width_height(content_rect)
        except Exception as exc:
            try:
                fallback_rect = get_objc_property(sc_window, "frame")
                content_w, content_h = cgrect_width_height(fallback_rect)
                point_pixel_scale = float(get_objc_property(content_filter, "pointPixelScale"))
            except Exception as fallback_exc:
                raise CaptureUnavailableError(
                    "ScreenCaptureKit 의 창 크기 정보를 해석하지 못했습니다. "
                    f"contentRect={content_rect!r} error={exc}; frame fallback error={fallback_exc}"
                ) from fallback_exc

        output_w = max(1, int(round(content_w * point_pixel_scale)))
        output_h = max(1, int(round(content_h * point_pixel_scale)))

        config = ScreenCaptureKit.SCStreamConfiguration.alloc().init()
        config.setValue_forKey_(False, "showsCursor")
        config.setValue_forKey_(True, "ignoreShadowsSingleWindow")
        config.setValue_forKey_(output_w, "width")
        config.setValue_forKey_(output_h, "height")

        result: Dict[str, Any] = {}
        done = threading.Event()

        def handler(image, error) -> None:
            result["image"] = image
            result["error"] = error
            done.set()

        ScreenCaptureKit.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
            content_filter,
            config,
            handler,
        )
        spin_cocoa_runloop(done, timeout=6.0, context="ScreenCaptureKit screenshot")

        error = result.get("error")
        image = result.get("image")
        if error is not None:
            self._sc_window = None
            raise CaptureUnavailableError(f"ScreenCaptureKit 창 캡처 오류: {error}")
        if image is None:
            self._sc_window = None
            raise CaptureUnavailableError(
                "ScreenCaptureKit 이 빈 이미지를 반환했습니다. 창이 최소화되었거나 캡처 권한이 없을 수 있습니다."
            )
        return cgimage_to_bgr(image)

    def _get_sc_window(self, force_refresh: bool = False):
        if force_refresh:
            self._sc_window = None
        if self._sc_window is not None:
            return self._sc_window

        shareable_content = fetch_shareable_content(on_screen_windows_only=False)
        windows = get_objc_property(shareable_content, "windows")
        for window in windows:
            try:
                if int(get_objc_property(window, "windowID")) == self.window_id:
                    self._sc_window = window
                    return window
            except Exception:
                continue
        return None

    def _crop_full_window(self, full_window: np.ndarray) -> np.ndarray:
        if self.norm_roi is None:
            raise RuntimeError("창 내부 ROI 가 아직 설정되지 않았습니다.")

        h, w = full_window.shape[:2]
        x_frac, y_frac, w_frac, h_frac = self.norm_roi

        left = int(round(x_frac * w))
        top = int(round(y_frac * h))
        right = int(round((x_frac + w_frac) * w))
        bottom = int(round((y_frac + h_frac) * h))

        left = max(0, min(left, w - 1))
        top = max(0, min(top, h - 1))
        right = max(left + 1, min(right, w))
        bottom = max(top + 1, min(bottom, h))
        return full_window[top:bottom, left:right].copy()


class SlideCaptureEngine:
    def __init__(
        self,
        capture_source: CaptureSource,
        output_dir: Path,
        config: Config,
        mode: str = "slide",
        show_preview: bool = False,
        make_pdf: bool = False,
        skip_duplicate_slides: bool = True,
        dedupe_pdf: bool = True,
    ) -> None:
        self.capture_source = capture_source
        self.output_dir = output_dir
        self.config = config
        self.mode = mode
        self.show_preview = show_preview
        self.make_pdf = make_pdf
        self.skip_duplicate_slides = skip_duplicate_slides
        self.dedupe_pdf = dedupe_pdf

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.output_dir / "captures.csv"
        self.duplicate_meta_path = self.output_dir / "duplicates.csv"

        self.reference: Optional[FrameRep] = None
        self.previous: Optional[FrameRep] = None
        self.candidate: Optional[Candidate] = None
        self.capture_count = 0
        self.last_save_monotonic = -10_000.0
        self.saved_paths: List[Path] = []
        self.saved_slide_records: List[SavedSlideRecord] = []
        self.duplicate_skip_count = 0
        self._preview_window_initialized = False
        self.stopper = GracefulStop()
        self.last_capture_error_log_monotonic = -10_000.0
        self._needs_resync_after_wait = False
        self._pause_logged = False

        self._init_metadata_csv()
        self._init_duplicate_csv()

    def _init_metadata_csv(self) -> None:
        if self.meta_path.exists():
            return
        with self.meta_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "index",
                    "saved_at_iso",
                    "elapsed_seconds",
                    "filename",
                    "frame_width",
                    "frame_height",
                    "source_type",
                    "capture_backend",
                    "window_id",
                    "window_owner",
                    "window_title",
                    "region_left",
                    "region_top",
                    "region_width",
                    "region_height",
                    "region_units",
                    "mode",
                ]
            )

    def _init_duplicate_csv(self) -> None:
        if self.duplicate_meta_path.exists():
            return
        with self.duplicate_meta_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "detected_at_iso",
                    "elapsed_seconds",
                    "duplicate_of_index",
                    "duplicate_of_filename",
                    "hash_distance",
                    "ssim",
                    "area",
                    "block_ratio",
                    "mode",
                ]
            )

    def _trigger_threshold(self) -> TriggerThreshold:
        return self.config.detailed_mode if self.mode == "detailed" else self.config.slide_mode

    def preprocess(self, frame_bgr: np.ndarray) -> FrameRep:
        h, w = frame_bgr.shape[:2]
        target_w = min(self.config.preprocess_width, w)
        target_h = max(1, int(h * target_w / w))
        small = cv2.resize(frame_bgr, (target_w, target_h), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        return FrameRep(small_bgr=small, blur_gray=blur)

    def compare(self, a: FrameRep, b: FrameRep) -> Metrics:
        score = float(structural_similarity(a.blur_gray, b.blur_gray, data_range=255))
        diff = cv2.absdiff(a.blur_gray, b.blur_gray)
        _, mask = cv2.threshold(diff, self.config.diff_threshold, 255, cv2.THRESH_BINARY)
        opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        area = float(opened.mean() / 255.0)

        h, w = opened.shape
        rows = self.config.block_rows
        cols = self.config.block_cols
        block_hits = 0
        total_blocks = rows * cols
        block_activation_threshold = 0.08

        for r in range(rows):
            y0 = int(r * h / rows)
            y1 = int((r + 1) * h / rows)
            for c in range(cols):
                x0 = int(c * w / cols)
                x1 = int((c + 1) * w / cols)
                region = opened[y0:y1, x0:x1]
                if region.size == 0:
                    continue
                if float(region.mean() / 255.0) >= block_activation_threshold:
                    block_hits += 1

        return Metrics(ssim=score, area=area, block_ratio=block_hits / total_blocks)

    def _compute_frame_hash(self, rep: FrameRep) -> int:
        reduced = cv2.resize(rep.blur_gray, (9, 8), interpolation=cv2.INTER_AREA)
        diff = reduced[:, 1:] > reduced[:, :-1]
        hash_value = 0
        for bit in diff.flatten():
            hash_value = (hash_value << 1) | int(bool(bit))
        return hash_value

    @staticmethod
    def _hamming_distance(a: int, b: int) -> int:
        return int((a ^ b).bit_count())

    def _find_duplicate_saved_slide(
        self,
        rep: FrameRep,
        records: Optional[List[SavedSlideRecord]] = None,
    ) -> Optional[DuplicateMatch]:
        source_records = self.saved_slide_records if records is None else records
        if not source_records:
            return None

        current_hash = self._compute_frame_hash(rep)
        best: Optional[DuplicateMatch] = None
        best_score: Optional[Tuple[float, float, float]] = None

        for record in source_records:
            metrics = self.compare(record.rep, rep)
            hash_distance = self._hamming_distance(current_hash, record.frame_hash)
            if (
                metrics.area <= self.config.duplicate_area_max
                and metrics.block_ratio <= self.config.duplicate_block_ratio_max
                and metrics.ssim >= self.config.duplicate_ssim_min
                and hash_distance <= self.config.duplicate_hash_distance_max
            ):
                score = (metrics.ssim, -metrics.area, -float(hash_distance))
                if best is None or score > best_score:
                    best = DuplicateMatch(record=record, metrics=metrics, hash_distance=hash_distance)
                    best_score = score
        return best

    def _log_duplicate_skip(self, duplicate: DuplicateMatch, saved_at: float) -> None:
        self.duplicate_skip_count += 1
        with self.duplicate_meta_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    datetime.now().isoformat(timespec="seconds"),
                    f"{saved_at:.2f}",
                    duplicate.record.index,
                    duplicate.record.path.name,
                    duplicate.hash_distance,
                    f"{duplicate.metrics.ssim:.4f}",
                    f"{duplicate.metrics.area:.4f}",
                    f"{duplicate.metrics.block_ratio:.4f}",
                    self.mode,
                ]
            )
        print(
            "[duplicate] 기존 슬라이드와 동일해 저장하지 않았습니다: "
            f"keep={duplicate.record.path.name} ssim={duplicate.metrics.ssim:.4f} "
            f"area={duplicate.metrics.area:.4f} blocks={duplicate.metrics.block_ratio:.4f}"
        )

    def _significant_change(self, ref_metrics: Metrics, trigger: TriggerThreshold) -> bool:
        return (
            ref_metrics.area >= trigger.area
            or ref_metrics.block_ratio >= trigger.block_ratio
            or ref_metrics.ssim <= trigger.ssim
        )

    def _abrupt_change(self, prev_metrics: Metrics) -> bool:
        return (
            prev_metrics.area >= self.config.abrupt_area
            or prev_metrics.block_ratio >= self.config.abrupt_block_ratio
            or prev_metrics.ssim <= self.config.abrupt_ssim
        )

    def _stable_relative_to_previous(self, prev_metrics: Metrics) -> bool:
        return (
            prev_metrics.area <= self.config.stable_inter_area
            and prev_metrics.ssim >= self.config.stable_inter_ssim
        )

    def _should_start_candidate(
        self,
        now: float,
        ref_metrics: Metrics,
        prev_metrics: Metrics,
    ) -> bool:
        trigger = self._trigger_threshold()
        significant = self._significant_change(ref_metrics, trigger)
        widespread = ref_metrics.block_ratio >= trigger.block_ratio
        abrupt = self._abrupt_change(prev_metrics)

        if not significant:
            return False

        # Right after a save, require a wider/global change so pen strokes do not retrigger.
        in_cooldown = (now - self.last_save_monotonic) < self.config.min_save_gap_seconds
        if in_cooldown and not widespread:
            return False

        return widespread or abrupt

    def _select_earliest_clean_frame(self, final_rep: FrameRep, history: List[HistoryItem]) -> HistoryItem:
        chosen = history[-1]
        for item in history:
            metrics = self.compare(item.rep, final_rep)
            if metrics.area <= self.config.final_match_area and metrics.ssim >= self.config.final_match_ssim:
                chosen = item
                break
        return chosen

    def _save_frame(
        self,
        frame_bgr: np.ndarray,
        saved_at: float,
        rep: Optional[FrameRep] = None,
    ) -> Optional[Path]:
        rep = rep if rep is not None else self.preprocess(frame_bgr)
        duplicate = self._find_duplicate_saved_slide(rep)
        if duplicate is not None and self.skip_duplicate_slides:
            self._log_duplicate_skip(duplicate, saved_at)
            return None

        self.capture_count += 1
        wall_clock = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"slide_{self.capture_count:04d}_{wall_clock}.png"
        out_path = self.output_dir / filename
        cv2.imwrite(str(out_path), frame_bgr)

        desc = self.capture_source.descriptor()
        h, w = frame_bgr.shape[:2]
        with self.meta_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    self.capture_count,
                    datetime.now().isoformat(timespec="seconds"),
                    f"{saved_at:.2f}",
                    filename,
                    w,
                    h,
                    desc.source_type,
                    desc.backend,
                    desc.window_id if desc.window_id is not None else "",
                    desc.window_owner,
                    desc.window_title,
                    desc.region_left,
                    desc.region_top,
                    desc.region_width,
                    desc.region_height,
                    desc.region_units,
                    self.mode,
                ]
            )

        self.saved_paths.append(out_path)
        self.saved_slide_records.append(
            SavedSlideRecord(
                index=self.capture_count,
                path=out_path,
                rep=rep,
                saved_at=saved_at,
                frame_hash=self._compute_frame_hash(rep),
            )
        )
        self.last_save_monotonic = saved_at
        print(f"[saved] {filename}  (elapsed={saved_at:.2f}s)")
        return out_path

    def _ensure_preview_window(self) -> None:
        if not self.show_preview or self._preview_window_initialized:
            return
        cv2.namedWindow(PREVIEW_WINDOW_NAME, cv2.WINDOW_NORMAL)
        self._preview_window_initialized = True

    def _preview_key_requests_quit(self, key: int) -> bool:
        if key < 0:
            return False
        low = key & 0xFF
        return low in {ord("q"), ord("Q"), 27}

    def _process_preview_events(self, max_wait_seconds: float, min_polls: int = 1) -> bool:
        if not self.show_preview:
            return False

        self._ensure_preview_window()
        deadline = time.monotonic() + max(0.0, max_wait_seconds)
        polls = 0
        while polls < min_polls or time.monotonic() < deadline:
            polls += 1
            remaining = max(0.0, deadline - time.monotonic())
            delay_ms = 1 if remaining <= 0 else max(1, min(25, int(round(remaining * 1000))))
            key = cv2.waitKeyEx(delay_ms)
            if self._preview_key_requests_quit(key):
                self.stopper.stop = True
                return True
            try:
                if cv2.getWindowProperty(PREVIEW_WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                    self.stopper.stop = True
                    return True
            except cv2.error:
                pass
        return False

    def _sleep_with_preview_events(self, seconds: float) -> bool:
        if self.show_preview:
            return self._process_preview_events(seconds, min_polls=1)
        if seconds > 0:
            time.sleep(seconds)
        return False

    def _draw_preview(
        self,
        frame_bgr: np.ndarray,
        elapsed: float,
        ref_metrics: Optional[Metrics],
        prev_metrics: Optional[Metrics],
    ) -> None:
        if not self.show_preview:
            return

        self._ensure_preview_window()
        preview = frame_bgr.copy()
        desc = self.capture_source.descriptor()
        lines = [
            f"elapsed: {elapsed:.1f}s",
            f"saved: {self.capture_count}",
            f"mode: {self.mode}",
            f"source: {desc.source_type}/{desc.backend}",
            f"candidate: {'yes' if self.candidate else 'no'}",
            "q / Q / Esc: quit",
        ]
        if ref_metrics:
            lines.append(
                f"vs saved ref -> ssim={ref_metrics.ssim:.3f} area={ref_metrics.area:.3f} blocks={ref_metrics.block_ratio:.3f}"
            )
        if prev_metrics:
            lines.append(
                f"vs prev     -> ssim={prev_metrics.ssim:.3f} area={prev_metrics.area:.3f} blocks={prev_metrics.block_ratio:.3f}"
            )

        y = 24
        for line in lines:
            cv2.putText(
                preview,
                line,
                (12, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 255, 0),
                1,
                cv2.LINE_AA,
            )
            y += 22

        cv2.imshow(PREVIEW_WINDOW_NAME, preview)

    def run(self) -> None:
        started_at = time.monotonic()
        print("[start] Ctrl+C 로 종료합니다.")
        if self.show_preview:
            print("[preview] 미리보기 창에서 q / Q / Esc 를 누르거나 창을 닫으면 종료됩니다.")

        try:
            while not self.stopper.stop:
                if self.stopper.paused:
                    self.candidate = None
                    self.previous = None
                    self._needs_resync_after_wait = True
                    if not self._pause_logged:
                        print("[pause] 캡처를 일시정지했습니다.")
                        self._pause_logged = True
                    if self._sleep_with_preview_events(self.config.sample_interval):
                        break
                    continue

                if self._pause_logged:
                    print("[pause] 캡처를 다시 시작합니다.")
                    self._pause_logged = False

                loop_started = time.monotonic()
                try:
                    current_frame = self.capture_source.capture_frame()
                except CaptureUnavailableError as exc:
                    now = time.monotonic()
                    self.candidate = None
                    self.previous = None
                    self._needs_resync_after_wait = True
                    if now - self.last_capture_error_log_monotonic >= 5.0:
                        print(f"[wait] {exc}")
                        self.last_capture_error_log_monotonic = now
                    if self._sleep_with_preview_events(self.config.sample_interval):
                        break
                    continue

                elapsed = loop_started - started_at
                current_rep = self.preprocess(current_frame)

                ref_metrics: Optional[Metrics] = None
                prev_metrics: Optional[Metrics] = None

                if self.reference is None:
                    self.reference = current_rep
                    self.previous = current_rep
                    self._save_frame(current_frame, elapsed, rep=current_rep)
                    self._draw_preview(current_frame, elapsed, None, None)
                else:
                    ref_metrics = self.compare(self.reference, current_rep)

                    if self.previous is None or self._needs_resync_after_wait:
                        self.previous = current_rep
                        self.candidate = None
                        self._needs_resync_after_wait = False
                        self._draw_preview(current_frame, elapsed, ref_metrics, None)
                    else:
                        prev_metrics = self.compare(self.previous, current_rep)

                        if self.candidate is None:
                            if self._should_start_candidate(elapsed, ref_metrics, prev_metrics):
                                self.candidate = Candidate(started_at=elapsed)
                                self.candidate.history.append(
                                    HistoryItem(timestamp=elapsed, frame_bgr=current_frame.copy(), rep=current_rep)
                                )
                        else:
                            self.candidate.history.append(
                                HistoryItem(timestamp=elapsed, frame_bgr=current_frame.copy(), rep=current_rep)
                            )

                            if self._stable_relative_to_previous(prev_metrics):
                                self.candidate.stable_count += 1
                            else:
                                self.candidate.stable_count = 0

                            candidate_age = elapsed - self.candidate.started_at
                            if (
                                self.candidate.stable_count >= self.config.settle_frames
                                and candidate_age >= self.config.min_candidate_seconds
                            ):
                                chosen = self._select_earliest_clean_frame(current_rep, self.candidate.history)
                                self.reference = chosen.rep
                                self._save_frame(chosen.frame_bgr, chosen.timestamp, rep=chosen.rep)
                                self.candidate = None
                            elif candidate_age >= self.config.max_candidate_seconds:
                                # If a candidate never settles, drop it instead of saving noisy frames forever.
                                self.candidate = None

                        self.previous = current_rep
                        self._draw_preview(current_frame, elapsed, ref_metrics, prev_metrics)

                elapsed_loop = time.monotonic() - loop_started
                sleep_for = max(0.0, self.config.sample_interval - elapsed_loop)
                if self._sleep_with_preview_events(sleep_for):
                    break
        finally:
            try:
                self.capture_source.close()
            finally:
                if self.show_preview:
                    cv2.destroyAllWindows()

        if self.make_pdf:
            self._make_pdf()

        print(f"[done] 총 {self.capture_count}장 저장")
        if self.duplicate_skip_count:
            print(f"[done] 중복 {self.duplicate_skip_count}장은 저장하지 않았습니다.")
        print(f"[output] {self.output_dir}")

    def _build_pdf_page_paths(self) -> List[Path]:
        if not self.saved_paths:
            return []
        if not self.dedupe_pdf:
            return list(self.saved_paths)

        unique_records: List[SavedSlideRecord] = []
        unique_paths: List[Path] = []
        removed_count = 0

        for path in self.saved_paths:
            frame_bgr = cv2.imread(str(path))
            if frame_bgr is None:
                print(f"[pdf] 이미지를 다시 읽지 못해 제외합니다: {path.name}")
                continue

            rep = self.preprocess(frame_bgr)
            duplicate = self._find_duplicate_saved_slide(rep, records=unique_records)
            if duplicate is not None:
                removed_count += 1
                print(
                    "[pdf] 중복 페이지 제외: "
                    f"drop={path.name} keep={duplicate.record.path.name} "
                    f"ssim={duplicate.metrics.ssim:.4f} area={duplicate.metrics.area:.4f}"
                )
                continue

            unique_paths.append(path)
            unique_records.append(
                SavedSlideRecord(
                    index=len(unique_paths),
                    path=path,
                    rep=rep,
                    saved_at=0.0,
                    frame_hash=self._compute_frame_hash(rep),
                )
            )

        if removed_count:
            print(f"[pdf] 중복 {removed_count}장을 제외하고 {len(unique_paths)}장으로 PDF를 만듭니다.")
        return unique_paths

    def _make_pdf(self) -> None:
        pdf_page_paths = self._build_pdf_page_paths()
        if not pdf_page_paths:
            print("[pdf] 저장된 이미지가 없어서 PDF를 만들지 않았습니다.")
            return

        pdf_path = self.output_dir / "slides.pdf"
        if img2pdf is not None:
            with pdf_path.open("wb") as f:
                f.write(img2pdf.convert([str(path) for path in pdf_page_paths]))
            print(f"[pdf] {pdf_path.name} 생성 완료 (img2pdf / 무손실 경로, {len(pdf_page_paths)}장)")
            return

        if Image is None:
            print(
                "[pdf] img2pdf 와 Pillow 가 없어 PDF 생성을 건너뜁니다. "
                "pip install img2pdf pillow 후 다시 실행하세요."
            )
            return

        print("[pdf] img2pdf 가 없어 Pillow 로 대체합니다. 이 경로는 PDF 내부에서 JPEG 재인코딩이 일어날 수 있습니다.")
        images = []
        for path in pdf_page_paths:
            img = Image.open(path).convert("RGB")
            images.append(img)
        head, *tail = images
        head.save(pdf_path, save_all=True, append_images=tail)
        print(f"[pdf] {pdf_path.name} 생성 완료 (Pillow 대체 경로, {len(pdf_page_paths)}장)")


def dict_get(mapping: Dict[str, Any], *keys: Any, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
        key_text = str(key)
        if key_text in mapping:
            return mapping[key_text]
    return default


# OpenCV ROI selection uses screen pixels, but huge Retina images do not fit. Downscale only for the selector UI.
def select_roi_on_image(image_bgr: np.ndarray, window_name: str) -> Tuple[int, int, int, int]:
    h, w = image_bgr.shape[:2]
    max_preview_w = 1600
    max_preview_h = 1000
    scale = min(max_preview_w / float(w), max_preview_h / float(h), 1.0)

    if scale < 1.0:
        preview = cv2.resize(
            image_bgr,
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        preview = image_bgr

    try:
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        ph, pw = preview.shape[:2]
        cv2.resizeWindow(window_name, pw, ph)
        x, y, roi_w, roi_h = cv2.selectROI(window_name, preview, showCrosshair=True, fromCenter=False)
    finally:
        cv2.destroyAllWindows()

    if roi_w <= 0 or roi_h <= 0:
        raise RuntimeError("영역 선택이 취소되었습니다.")

    if scale < 1.0:
        x = int(round(x / scale))
        y = int(round(y / scale))
        roi_w = int(round(roi_w / scale))
        roi_h = int(round(roi_h / scale))

    x = max(0, min(int(x), w - 1))
    y = max(0, min(int(y), h - 1))
    roi_w = max(1, min(int(roi_w), w - x))
    roi_h = max(1, min(int(roi_h), h - y))
    return x, y, roi_w, roi_h


# Full virtual desktop capture remains as the cross-platform fallback.
def grab_full_desktop() -> Tuple[np.ndarray, Dict[str, int]]:
    with mss() as sct:
        monitor = sct.monitors[0].copy()
        shot = sct.grab(monitor)
        img = np.array(shot)
        bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        return bgr, monitor


def select_screen_region_interactively() -> Dict[str, int]:
    screen_bgr, monitor = grab_full_desktop()
    window_name = "영역 선택: 강의 영상 부분만 드래그 후 Enter / Space, 취소는 c"
    x, y, w, h = select_roi_on_image(screen_bgr, window_name)
    return {
        "left": int(monitor["left"] + x),
        "top": int(monitor["top"] + y),
        "width": int(w),
        "height": int(h),
    }


def parse_roi(roi_text: str) -> Dict[str, int]:
    parts = [p.strip() for p in roi_text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--roi 는 left,top,width,height 형식이어야 합니다.")
    try:
        left, top, width, height = map(int, parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--roi 값은 정수여야 합니다.") from exc

    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("width 와 height 는 1 이상이어야 합니다.")
    return {"left": left, "top": top, "width": width, "height": height}


def save_capture_json(payload: Dict[str, Any], output_dir: Path) -> None:
    path = output_dir / "capture_source.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def spin_cocoa_runloop(done: threading.Event, timeout: float, context: str) -> None:
    deadline = time.monotonic() + timeout
    while not done.is_set():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{context} 응답 대기 시간이 초과되었습니다.")
        if NSRunLoop is not None and NSDate is not None:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
        else:
            time.sleep(0.05)


def fetch_shareable_content(on_screen_windows_only: bool) -> Any:
    if ScreenCaptureKit is None:
        raise CaptureUnavailableError("ScreenCaptureKit 이 설치되어 있지 않습니다.")

    result: Dict[str, Any] = {}
    done = threading.Event()

    def handler(content, error) -> None:
        result["content"] = content
        result["error"] = error
        done.set()

    ScreenCaptureKit.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
        False,
        bool(on_screen_windows_only),
        handler,
    )
    spin_cocoa_runloop(done, timeout=6.0, context="SCShareableContent")

    error = result.get("error")
    if error is not None:
        raise CaptureUnavailableError(f"SCShareableContent 오류: {error}")
    content = result.get("content")
    if content is None:
        raise CaptureUnavailableError("SCShareableContent 가 비어 있습니다.")
    return content


def get_objc_property(obj: Any, name: str) -> Any:
    attr = getattr(obj, name, None)
    if attr is not None:
        try:
            return attr() if callable(attr) else attr
        except TypeError:
            return attr
        except Exception:
            pass
    if hasattr(obj, "valueForKey_"):
        return obj.valueForKey_(name)
    raise AttributeError(f"{type(obj).__name__} 에서 Objective-C 속성 {name!r} 를 찾지 못했습니다.")


def unwrap_objc_rect_value(rect_like: Any) -> Any:
    if rect_like is None:
        return None
    for method_name in ("CGRectValue", "rectValue"):
        method = getattr(rect_like, method_name, None)
        if method is None:
            continue
        try:
            unwrapped = method() if callable(method) else method
            if unwrapped is not None:
                return unwrapped
        except Exception:
            continue
    return rect_like


def cgsize_width_height(size_like: Any) -> Tuple[float, float]:
    size_like = unwrap_objc_rect_value(size_like)

    try:
        return float(size_like.width), float(size_like.height)  # type: ignore[attr-defined]
    except Exception:
        pass

    if hasattr(size_like, "_asdict"):
        try:
            as_dict = size_like._asdict()
            if "width" in as_dict and "height" in as_dict:
                return float(as_dict["width"]), float(as_dict["height"])
        except Exception:
            pass

    try:
        if len(size_like) == 2:
            return float(size_like[0]), float(size_like[1])
    except Exception:
        pass

    text = repr(size_like)
    numbers = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
    if len(numbers) >= 2:
        return float(numbers[-2]), float(numbers[-1])

    raise RuntimeError(f"CGSize 크기를 해석하지 못했습니다: {size_like!r}")


def cgrect_width_height(rect: Any) -> Tuple[float, float]:
    rect = unwrap_objc_rect_value(rect)

    try:
        return cgsize_width_height(rect.size)  # type: ignore[attr-defined]
    except Exception:
        pass

    try:
        if hasattr(rect, "_asdict"):
            as_dict = rect._asdict()
            if "size" in as_dict:
                return cgsize_width_height(as_dict["size"])
            if "width" in as_dict and "height" in as_dict:
                return float(as_dict["width"]), float(as_dict["height"])
    except Exception:
        pass

    try:
        if len(rect) == 2:
            return cgsize_width_height(rect[1])  # PyObjC sometimes exposes CGRect as ((x, y), (w, h)).
        if len(rect) == 4:
            return float(rect[2]), float(rect[3])
    except Exception:
        pass

    if AppKit is not None:
        for func_name in ("NSWidth", "NSHeight"):
            if not hasattr(AppKit, func_name):
                break
        else:
            try:
                return float(AppKit.NSWidth(rect)), float(AppKit.NSHeight(rect))
            except Exception:
                pass

    if Quartz is not None:
        try:
            return float(Quartz.CGRectGetWidth(rect)), float(Quartz.CGRectGetHeight(rect))
        except Exception:
            pass

    text = repr(rect)
    numbers = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
    if len(numbers) >= 4:
        return float(numbers[-2]), float(numbers[-1])

    raise RuntimeError(f"CGRect 크기를 해석하지 못했습니다: {rect!r}")


# PNG serialization via NSBitmapImageRep avoids hard-coding the CGImage's underlying pixel byte order.
def cgimage_to_bgr(image: Any) -> np.ndarray:
    if AppKit is None:
        raise RuntimeError("macOS CGImage 변환에는 pyobjc-framework-Cocoa 가 필요합니다.")

    rep = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
    png_data = rep.representationUsingType_properties_(AppKit.NSPNGFileType, None)
    if png_data is None:
        raise RuntimeError("CGImage 를 PNG 로 직렬화하지 못했습니다.")
    encoded = bytes(png_data)
    arr = np.frombuffer(encoded, dtype=np.uint8)
    decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if decoded is None:
        raise RuntimeError("PNG 디코딩에 실패했습니다.")
    return decoded


def list_candidate_windows(owner_filter: Optional[str], title_filter: Optional[str]) -> List[Dict[str, Any]]:
    if Quartz is None:
        raise RuntimeError("창 목록 조회에는 pyobjc-framework-Quartz 가 필요합니다.")

    info_list = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    if info_list is None:
        return []

    owner_filter_lc = (owner_filter or "").strip().lower()
    title_filter_lc = (title_filter or "").strip().lower()
    windows: List[Dict[str, Any]] = []
    for info in list(info_list):
        layer = int(dict_get(info, Quartz.kCGWindowLayer, "kCGWindowLayer", default=0) or 0)
        if layer != 0:
            continue

        owner = str(dict_get(info, Quartz.kCGWindowOwnerName, "kCGWindowOwnerName", default="") or "")
        title = str(dict_get(info, Quartz.kCGWindowName, "kCGWindowName", default="") or "")
        bounds = dict_get(info, Quartz.kCGWindowBounds, "kCGWindowBounds", default={}) or {}
        alpha = float(dict_get(info, Quartz.kCGWindowAlpha, "kCGWindowAlpha", default=1.0) or 1.0)
        is_onscreen = bool(dict_get(info, Quartz.kCGWindowIsOnscreen, "kCGWindowIsOnscreen", default=True))
        if not is_onscreen or alpha <= 0.0:
            continue

        try:
            width = int(bounds.get("Width", 0))
            height = int(bounds.get("Height", 0))
            left = int(bounds.get("X", 0))
            top = int(bounds.get("Y", 0))
        except Exception:
            continue

        if width < 200 or height < 120:
            continue

        if owner_filter_lc and owner_filter_lc not in owner.lower():
            continue
        if title_filter_lc and title_filter_lc not in title.lower():
            continue

        window_id = int(dict_get(info, Quartz.kCGWindowNumber, "kCGWindowNumber", default=0) or 0)
        if window_id <= 0:
            continue

        windows.append(
            {
                "window_id": window_id,
                "window_owner": owner,
                "window_title": title,
                "left": left,
                "top": top,
                "width": width,
                "height": height,
                "area": width * height,
            }
        )

    windows.sort(key=lambda item: item["area"], reverse=True)
    return windows


def print_candidate_windows(windows: List[Dict[str, Any]]) -> None:
    if not windows:
        print("[windows] 표시 가능한 창을 찾지 못했습니다.")
        return

    print("[windows] 후보 창 목록")
    for idx, item in enumerate(windows, start=1):
        title = item["window_title"] or "(제목 없음)"
        print(
            f"  {idx:02d}. id={item['window_id']}  owner={item['window_owner']}  "
            f"size={item['width']}x{item['height']}  title={title}"
        )


def choose_target_window_interactively(
    owner_filter: Optional[str],
    title_filter: Optional[str],
) -> Dict[str, Any]:
    while True:
        candidates = list_candidate_windows(owner_filter, title_filter)
        if not candidates and owner_filter == "Google Chrome":
            candidates = list_candidate_windows("Chrome", title_filter)
        if not candidates:
            raise RuntimeError(
                "선택할 창을 찾지 못했습니다. Chrome 창을 앞으로 띄운 뒤 다시 시도하세요."
            )

        print_candidate_windows(candidates)
        print("[windows] 번호를 입력하세요. Enter=1번, r=새로고침, q=취소")
        try:
            answer = input("> 선택: ").strip()
        except EOFError:
            answer = ""

        if answer == "":
            return candidates[0]
        lowered = answer.lower()
        if lowered in {"q", "quit", "exit"}:
            raise RuntimeError("창 선택이 취소되었습니다.")
        if lowered in {"r", "refresh", "reload"}:
            print()
            continue

        try:
            number = int(answer)
        except ValueError:
            print("[windows] 숫자 번호를 입력해 주세요.\n")
            continue

        if 1 <= number <= len(candidates):
            return candidates[number - 1]

        for item in candidates:
            if int(item["window_id"]) == number:
                return item

        print("[windows] 범위를 벗어난 번호입니다.\n")


# Build the capture source using the best macOS backend available, while keeping the previous screen ROI mode as fallback.
def build_capture_source(args: argparse.Namespace, output_dir: Path) -> CaptureSource:
    capture_source_mode = resolve_capture_source_mode(args.capture_source)

    if capture_source_mode == "window":
        if Quartz is None:
            raise RuntimeError(
                "window 모드에는 pyobjc-framework-Quartz 가 필요합니다. requirements.txt 를 설치하세요."
            )

        owner_filter = normalize_optional_string(args.window_owner)
        title_filter = normalize_optional_string(args.window_title)

        if args.list_windows:
            print_candidate_windows(list_candidate_windows(owner_filter, title_filter))
            raise SystemExit(0)

        chosen_window = resolve_target_window(
            args.window_id,
            owner_filter,
            title_filter,
            choose_window=args.choose_window,
        )
        source = MacWindowSource(
            window_id=int(chosen_window["window_id"]),
            window_owner=str(chosen_window["window_owner"]),
            window_title=str(chosen_window["window_title"]),
            backend=args.window_backend,
            pause_on_cursor_in_roi=not args.no_pause_on_cursor_in_roi,
        )
        print(
            f"[window] id={source.window_id} owner={source.window_owner} "
            f"title={source.window_title or '(제목 없음)'} backend={source.backend}"
        )
        print("[roi] Chrome 창 내부에서 강의 슬라이드 부분만 선택하세요.")
        preview = source.selection_preview()
        x, y, w, h = select_roi_on_image(
            preview,
            "창 내부 영역 선택: 슬라이드 부분만 드래그 후 Enter / Space, 취소는 c",
        )
        ph, pw = preview.shape[:2]
        source.set_roi_from_pixels(x, y, w, h, pw, ph)
        save_capture_json(source.to_json(), output_dir)
        return source

    if args.list_windows:
        # screen 모드에서 --list-windows 가 들어와도 사용자가 혼동하지 않도록 안내.
        print("[windows] screen 모드에서는 창 목록을 사용하지 않습니다. --capture-source window 와 함께 사용하세요.")
        raise SystemExit(0)

    if args.roi is not None:
        region = args.roi
    else:
        print("[roi] Chrome 창을 화면에 띄운 상태에서 강의 슬라이드 영역만 선택하세요.")
        region = select_screen_region_interactively()

    source = ScreenRegionSource(
        region,
        pause_on_cursor_in_roi=not args.no_pause_on_cursor_in_roi,
    )
    save_capture_json(source.to_json(), output_dir)
    print("[roi] left={left}, top={top}, width={width}, height={height}".format(**region))
    return source


def resolve_capture_source_mode(requested: str) -> str:
    requested = requested.lower()
    if requested not in {"auto", "window", "screen"}:
        raise RuntimeError(f"지원하지 않는 캡처 소스입니다: {requested}")

    if requested == "screen":
        return "screen"
    if requested == "window":
        return "window"

    if sys.platform == "darwin" and Quartz is not None:
        return "window"
    return "screen"


def normalize_optional_string(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def resolve_target_window(
    explicit_window_id: Optional[int],
    owner_filter: Optional[str],
    title_filter: Optional[str],
    choose_window: bool = False,
) -> Dict[str, Any]:
    if explicit_window_id is not None:
        explicit_window_id = int(explicit_window_id)
        all_windows = list_candidate_windows(None, None)
        for item in all_windows:
            if int(item["window_id"]) == explicit_window_id:
                return item

        filtered_candidates = list_candidate_windows(owner_filter, title_filter)
        if not filtered_candidates and owner_filter == "Google Chrome":
            # Be slightly more forgiving for Chromium-based variants.
            filtered_candidates = list_candidate_windows("Chrome", title_filter)

        if 1 <= explicit_window_id <= len(filtered_candidates):
            chosen = filtered_candidates[explicit_window_id - 1]
            print(
                f"[window] 입력값 {explicit_window_id} 을(를) 후보 번호로 해석해 "
                f"창 ID {chosen['window_id']} 를 선택합니다."
            )
            return chosen

        return {
            "window_id": explicit_window_id,
            "window_owner": owner_filter or "",
            "window_title": title_filter or "",
            "left": 0,
            "top": 0,
            "width": 0,
            "height": 0,
            "area": 0,
        }

    if choose_window:
        return choose_target_window_interactively(owner_filter, title_filter)

    candidates = list_candidate_windows(owner_filter, title_filter)
    if not candidates and owner_filter == "Google Chrome":
        # Be slightly more forgiving for Chromium-based variants.
        candidates = list_candidate_windows("Chrome", title_filter)
    if not candidates:
        raise RuntimeError(
            "대상 창을 찾지 못했습니다. Chrome 창을 앞으로 띄운 뒤 다시 실행하거나, "
            "--list-windows 로 후보를 확인한 다음 --window-id 로 지정하세요."
        )
    return candidates[0]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "강의 영상의 슬라이드가 바뀌는 시점만 자동 저장합니다. "
            "macOS 에서는 기본적으로 Chrome 창 자체를 고정 캡처하려고 시도하고, "
            "불가능하면 기존 화면 ROI 방식으로도 동작합니다."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("captures"),
        help="저장 폴더 (기본값: ./captures)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.60,
        help="샘플링 간격(초). 기본값: 0.60",
    )
    parser.add_argument(
        "--mode",
        choices=["slide", "detailed"],
        default="slide",
        help=(
            "slide=기본. 손글씨/포인터를 강하게 무시하고 큰 화면 전환 위주로 저장. "
            "detailed=작은 변경도 더 잘 잡지만 오탐이 늘 수 있음."
        ),
    )
    parser.add_argument(
        "--capture-source",
        choices=["auto", "window", "screen"],
        default="auto",
        help=(
            "auto=macOS 에서는 창 고정 캡처 우선, 그 외는 화면 ROI. "
            "window=macOS 창 고정 캡처 강제. screen=기존 화면 ROI 방식 강제."
        ),
    )
    parser.add_argument(
        "--window-backend",
        choices=["auto", "screencapturekit", "coregraphics"],
        default="auto",
        help=(
            "macOS window 모드에서 사용할 내부 백엔드. "
            "auto=가능하면 ScreenCaptureKit, 아니면 CoreGraphics."
        ),
    )
    parser.add_argument(
        "--window-owner",
        default="Google Chrome",
        help="macOS window 모드에서 앱 이름 필터. 기본값: Google Chrome",
    )
    parser.add_argument(
        "--window-title",
        default=None,
        help="macOS window 모드에서 창 제목 부분 문자열 필터",
    )
    parser.add_argument(
        "--window-id",
        type=int,
        default=None,
        help="macOS window 모드에서 대상 창 ID 직접 지정. 후보 목록 번호(1, 2, 3...)도 허용",
    )
    parser.add_argument(
        "--choose-window",
        action="store_true",
        help="macOS window 모드에서 캡처 시작 전에 현재 창 목록을 보여주고 번호로 선택",
    )
    parser.add_argument(
        "--list-windows",
        action="store_true",
        help="macOS 에서 현재 보이는 창 후보를 출력하고 종료",
    )
    parser.add_argument(
        "--roi",
        type=parse_roi,
        default=None,
        help="screen 모드에서 캡처 영역 직접 지정: left,top,width,height",
    )
    parser.add_argument(
        "--no-pause-on-cursor-in-roi",
        action="store_true",
        help="ROI 안에 마우스 커서가 들어와도 캡처를 일시정지하지 않음",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="실시간 미리보기 창 표시",
    )
    parser.add_argument(
        "--make-pdf",
        action="store_true",
        help="종료 시 저장된 이미지들을 slides.pdf 로 묶음 (img2pdf 권장)",
    )
    parser.add_argument(
        "--keep-duplicate-slides",
        action="store_true",
        help="반복 등장한 동일 슬라이드도 저장/ PDF 에 그대로 유지함 (기본값은 중복 제거)",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    config = Config(sample_interval=max(0.10, float(args.interval)))
    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    capture_source = build_capture_source(args, output_dir)
    if not args.no_pause_on_cursor_in_roi:
        print("[guard] 커서가 ROI 안에 들어오면 캡처를 일시정지합니다.")

    engine = SlideCaptureEngine(
        capture_source=capture_source,
        output_dir=output_dir,
        config=config,
        mode=args.mode,
        show_preview=args.preview,
        make_pdf=args.make_pdf,
        skip_duplicate_slides=not args.keep_duplicate_slides,
        dedupe_pdf=not args.keep_duplicate_slides,
    )
    engine.run()


if __name__ == "__main__":
    main()
