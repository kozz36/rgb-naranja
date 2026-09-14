#!/usr/bin/env python3
# THERMALRIGHT_DASHBOARD_MANAGED_COPY
"""Thermalright dashboard for the confirmed 87ad:70db PM=64/FBL=114 panel.

Run a bounded foreground session with ``--duration SECONDS`` (120 seconds by
default), or use ``--continuous`` only through the supplied *user* systemd unit.
``--preview`` is deliberately offline: it writes a synthetic 1600x720 DEMO PNG
without importing TRCC, reading sensors, or touching USB.  The installer copies
this script and the unit without changing service state by default; rerun it to
update a managed copy, use ``--start`` only when ready to enable/restart it, and
``--uninstall`` to stop/disable and remove its exact managed copies.

Live mode connects only to 87ad:70db, uses TRCC's native ``SendImage`` temporary
PNG pipeline, reads raw ``platform.sensors().read_all()`` values, and shows absent,
invalid, or non-finite readings as N/A.  Rendering remains capped at 1 Hz while
TRCC's raw-reading cache is approximately 2 seconds.  ``TRCC_DAEMON=1`` is refused
before TRCC boot because it selects a shared daemon proxy; the unit sets it to 0.
A Linux advisory lock in ``XDG_RUNTIME_DIR`` prevents a direct live invocation
from duplicating the unit, without a persistent PID file.

On KDE Wayland, live mode polls ``/usr/bin/kscreen-doctor --dpms show`` every two
monotonic seconds. Only a complete report of every actual output as ``off`` streams
one cached true-black PNG; any reported ``on`` restores the dashboard. A timeout,
command error, or malformed report is UNKNOWN and holds the last output (the initial
UNKNOWN output is the dashboard). This streams black pixels; it does not physically
power off monitors or control suspend. Lock, idle, and screensaver state are not
consulted, and this script never sends a firmware power command.

Install the separately reviewed USB rule as
``/etc/udev/rules.d/70-rgb-naranja-thermalright.rules``.  It uses active-session
``uaccess`` rather than global device permissions; do not use a global trigger.
The user unit follows the user-manager lifecycle: if linger or another user
session keeps that manager alive, it can remain running after graphical logout.
With no active seat ACL it fails cleanly and systemd retries later.  This animation
only changes rendered colors and placement; it neither changes panel brightness or
firmware nor guarantees OLED burn-in prevention or physical pixel refresh.
"""

from __future__ import annotations

import argparse
import colorsys
from collections import deque
import importlib.util
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from enum import Enum
import logging
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterator


DEVICE_KEY = "87ad:70db"
EXPECTED_PM_BYTE = 64
EXPECTED_FBL = 114
FRAME_SIZE = (1600, 720)
DEFAULT_DURATION_S = 120.0
RENDER_INTERVAL_S = 1.0
DPMS_POLL_INTERVAL_S = 2.0
DPMS_QUERY_TIMEOUT_S = 1.5
KSCREEN_DOCTOR_PATH = Path("/usr/bin/kscreen-doctor")
HUE_CYCLE_S = 900.0
SHIFT_LIMIT_PX = 6
TEMPERATURE_WINDOW_S = 300.0
MAX_TEMPERATURE_HISTORY_SAMPLES = int(TEMPERATURE_WINDOW_S / RENDER_INTERVAL_S) + 2
MAX_TEMPERATURE_SAMPLE_GAP_S = RENDER_INTERVAL_S * 3.0
TEMPERATURE_MIN_C = 0.0
TEMPERATURE_MAX_C = 110.0
LOCK_FILENAME = "thermalright-dashboard.lock"
BACKGROUND = "#000000"
CARD_BACKGROUND = "#000000"
HOSTNAME = socket.gethostname()
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
JPEG_TIMEOUT_S = 2.0


def _load_sibling(module_name: str, filename: str) -> Any:
    """Load an optional installed sibling without referring to checkout paths."""
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_DIRECTORY / filename)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling module {filename}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses in sibling modules resolve annotations through sys.modules.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class DashboardError(RuntimeError):
    """A safe live-mode failure which should result in a non-zero exit."""


class DpmsState(Enum):
    """The only display-power states trusted by the conservative KDE backend."""

    AWAKE = "awake"
    SLEEP = "sleep"
    UNKNOWN = "unknown"


_DPMS_RECORD = re.compile(r"dpms mode for screen (?P<screen>.+): (?P<mode>on|off)")


def parse_kscreen_dpms_stdout(stdout: object) -> DpmsState:
    """Classify only a complete, non-empty, non-duplicate KDE DPMS report."""
    if not isinstance(stdout, str) or not stdout:
        return DpmsState.UNKNOWN
    records: dict[str, str] = {}
    for line in stdout.splitlines():
        match = _DPMS_RECORD.fullmatch(line)
        if match is None:
            return DpmsState.UNKNOWN
        screen = match.group("screen")
        if not screen or screen in records:
            return DpmsState.UNKNOWN
        records[screen] = match.group("mode")
    if not records:
        return DpmsState.UNKNOWN
    return DpmsState.SLEEP if all(mode == "off" for mode in records.values()) else DpmsState.AWAKE


class KdeWaylandDpmsProvider:
    """Read actual KDE Wayland output DPMS state without changing the parent environment."""

    def __init__(
        self,
        *,
        runner: Callable[..., Any] | None = None,
        executable: Path = KSCREEN_DOCTOR_PATH,
    ) -> None:
        self._runner = subprocess.run if runner is None else runner
        self._executable = executable

    def read_state(self) -> DpmsState:
        """Return UNKNOWN rather than guessing when KDE's short query is unavailable."""
        environment = os.environ.copy()
        environment.update({"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "QT_QPA_PLATFORM": "wayland"})
        try:
            result = self._runner(
                [str(self._executable), "--dpms", "show"],
                shell=False,
                capture_output=True,
                text=True,
                timeout=DPMS_QUERY_TIMEOUT_S,
                env=environment,
            )
        except Exception:
            return DpmsState.UNKNOWN
        if getattr(result, "returncode", None) != 0:
            return DpmsState.UNKNOWN
        return parse_kscreen_dpms_stdout(getattr(result, "stdout", None))


@dataclass(frozen=True)
class MetricCard:
    temp: float | None
    usage: float | None
    power_w: float | None = None


@dataclass(frozen=True)
class RamUsage:
    used_mb: float | None
    total_mb: float | None
    percent: float | None


@dataclass(frozen=True)
class TemperatureSample:
    """One temperature observation at a monotonic elapsed timestamp."""

    elapsed_s: float
    value: float | None


@dataclass(frozen=True)
class DashboardSnapshot:
    cpu: MetricCard
    gpu: MetricCard
    ram: RamUsage
    demo: bool = False
    cpu_history: tuple[TemperatureSample, ...] = ()
    gpu_history: tuple[TemperatureSample, ...] = ()
    history_now_s: float | None = None


@dataclass(frozen=True)
class BurnInPhase:
    """Style-only phase derived from monotonic time, never from sensor values."""

    hue_degrees: float
    shift_x: int
    shift_y: int


@dataclass(frozen=True)
class DashboardPalette:
    accent: str
    text: str
    value: str
    border: str
    separator: str
    muted: str


@dataclass(frozen=True)
class LiveDependencies:
    """Lazy live-mode boundary, replaceable by stdlib-only unit-test fakes."""

    app_factory: Callable[[], Any]
    ensure_connected: Callable[[str], Any]
    send_image: Callable[[str, Path], Any]
    render_png: Callable[[DashboardSnapshot, Path, BurnInPhase], None]
    render_black_png: Callable[[Path], None]
    read_dpms_state: Callable[[], DpmsState]


def _finite_number(value: object) -> float | None:
    """Return a finite numeric reading, preserving missing or malformed as None."""
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)  # TRCC sources return int/float-like values.
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _power_w(value: object) -> float | None:
    """Accept only truthful, finite non-negative watt readings; zero is a valid reading."""
    numeric = _finite_number(value)
    return numeric if numeric is not None and numeric >= 0.0 else None


class TemperatureHistory:
    """Bounded render-time history; snapshots receive immutable tuples only."""

    def __init__(self, max_samples: int = MAX_TEMPERATURE_HISTORY_SAMPLES) -> None:
        if max_samples <= 0:
            raise DashboardError("temperature history size must be positive")
        self._max_samples = max_samples
        self._samples: deque[TemperatureSample] = deque()

    @property
    def samples(self) -> tuple[TemperatureSample, ...]:
        return tuple(self._samples)

    def record(self, elapsed_s: object, value: object) -> tuple[TemperatureSample, ...]:
        timestamp = _finite_number(elapsed_s)
        if timestamp is None:
            raise DashboardError("temperature history timestamp must be finite")
        if self._samples and timestamp < self._samples[-1].elapsed_s:
            raise DashboardError("temperature history timestamps must be monotonic")
        self._samples.append(TemperatureSample(timestamp, _finite_number(value)))
        cutoff = timestamp - TEMPERATURE_WINDOW_S
        while self._samples and self._samples[0].elapsed_s < cutoff:
            self._samples.popleft()
        while len(self._samples) > self._max_samples:
            self._samples.popleft()
        return self.samples


def prune_temperature_samples(
    samples: Sequence[TemperatureSample], *, now_s: float, window_s: float = TEMPERATURE_WINDOW_S
) -> tuple[TemperatureSample, ...]:
    """Keep chronological samples in the visible elapsed-time window."""
    now = _finite_number(now_s)
    if now is None or not math.isfinite(window_s) or window_s <= 0:
        raise DashboardError("temperature graph time window must be finite and positive")
    cutoff = now - window_s
    return tuple(
        sample
        for sample in samples
        if _finite_number(sample.elapsed_s) is not None and sample.elapsed_s >= cutoff
    )


def timestamp_to_x(
    elapsed_s: float, *, now_s: float, width: int, window_s: float = TEMPERATURE_WINDOW_S
) -> int:
    """Map an elapsed timestamp into a clamped elapsed-time graph coordinate."""
    timestamp = _finite_number(elapsed_s)
    now = _finite_number(now_s)
    if timestamp is None or now is None or not math.isfinite(window_s) or window_s <= 0:
        raise DashboardError("temperature graph timestamps must be finite")
    if width <= 0:
        raise DashboardError("temperature graph width must be positive")
    fraction = (timestamp - (now - window_s)) / window_s
    return round(min(1.0, max(0.0, fraction)) * width)


def temperature_to_y(value: float, *, top: int, height: int) -> tuple[int, bool]:
    """Map a temperature to the fixed 0--110 C scale and flag clamped values."""
    numeric = _finite_number(value)
    if numeric is None or height <= 0:
        raise DashboardError("temperature graph value and height must be valid")
    clamped = min(TEMPERATURE_MAX_C, max(TEMPERATURE_MIN_C, numeric))
    out_of_range = clamped != numeric
    fraction = (TEMPERATURE_MAX_C - clamped) / (TEMPERATURE_MAX_C - TEMPERATURE_MIN_C)
    return top + round(fraction * (height - 1)), out_of_range


def temperature_segments(
    samples: Sequence[TemperatureSample], *, now_s: float
) -> tuple[tuple[TemperatureSample, ...], ...]:
    """Return drawable runs, breaking at unavailable samples and long pauses."""
    segments: list[tuple[TemperatureSample, ...]] = []
    current: list[TemperatureSample] = []
    previous_s: float | None = None
    for sample in prune_temperature_samples(samples, now_s=now_s):
        timestamp = _finite_number(sample.elapsed_s)
        value = _finite_number(sample.value)
        if timestamp is None or value is None:
            if current:
                segments.append(tuple(current))
                current = []
            previous_s = None
            continue
        if previous_s is None or timestamp - previous_s > MAX_TEMPERATURE_SAMPLE_GAP_S:
            if current:
                segments.append(tuple(current))
            current = [TemperatureSample(timestamp, value)]
        else:
            current.append(TemperatureSample(timestamp, value))
        previous_s = timestamp
    if current:
        segments.append(tuple(current))
    return tuple(segments)


def snapshot_with_temperature_history(
    snapshot: DashboardSnapshot,
    *,
    elapsed_s: float,
    cpu_history: TemperatureHistory,
    gpu_history: TemperatureHistory,
) -> DashboardSnapshot:
    """Attach only this process's bounded observations to an immutable frame input."""
    return replace(
        snapshot,
        cpu_history=cpu_history.record(elapsed_s, snapshot.cpu.temp),
        gpu_history=gpu_history.record(elapsed_s, snapshot.gpu.temp),
        history_now_s=elapsed_s,
    )


def snapshot_from_readings(readings: object) -> DashboardSnapshot:
    """Map TRCC's raw normalized keys without inventing fallbacks or zeros.

    ``gpu:primary:*`` is produced by TRCC after its own discrete/vendor-aware GPU
    selection.  Intentionally do not fall back to ``gpu:0:*``: a primary GPU that
    cannot be measured is unavailable, not evidence that another GPU is primary.
    ``cpu:power`` is TRCC's summed readable RAPL package-domain power, averaged
    over its polling interval (about two seconds). ``gpu:primary:power`` is the
    selected GPU's watt reading (NVIDIA NVML on this host).
    """
    raw: Mapping[str, object] = readings if isinstance(readings, Mapping) else {}
    return DashboardSnapshot(
        cpu=MetricCard(
            temp=_finite_number(raw.get("cpu:temp")),
            usage=_finite_number(raw.get("cpu:usage")),
            power_w=_power_w(raw.get("cpu:power")),
        ),
        gpu=MetricCard(
            temp=_finite_number(raw.get("gpu:primary:temp")),
            usage=_finite_number(raw.get("gpu:primary:usage")),
            power_w=_power_w(raw.get("gpu:primary:power")),
        ),
        ram=RamUsage(
            used_mb=_finite_number(raw.get("memory:used")),
            total_mb=_finite_number(raw.get("memory:total")),
            percent=_finite_number(raw.get("memory:percent")),
        ),
    )


def demo_snapshot() -> DashboardSnapshot:
    """A synthetic snapshot used only by explicit offline preview mode."""
    return DashboardSnapshot(
        cpu=MetricCard(temp=58.0, usage=37.0, power_w=75.0),
        gpu=MetricCard(temp=63.0, usage=71.0, power_w=31.0),
        ram=RamUsage(used_mb=12_480.0, total_mb=32_768.0, percent=38.0),
        demo=True,
    )


class RawTrccSensorProvider:
    """Read TRCC's ``read_all`` mapping without the zero-filling ``discover`` API."""

    def __init__(self, sensors: object | None) -> None:
        self._sensors = sensors

    def readings(self) -> Mapping[str, object]:
        """Degrade sensor/NVML failures to an empty truthful mapping."""
        if self._sensors is None:
            return {}
        try:
            raw = self._sensors.read_all()
        except Exception:
            # The installed aggregator already isolates per-sensor failures.  Keep
            # this final boundary too, so a NVML/backend failure cannot end the UI.
            return {}
        return raw if isinstance(raw, Mapping) else {}


def format_temperature(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.0f}°C"


def format_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.0f}%"


def format_power(value: float | None) -> str:
    """Format a normalized per-device watt value without estimating unavailable data."""
    return "N/A" if value is None else f"{value:.0f} W"


def format_ram(value: RamUsage) -> str:
    if value.used_mb is None or value.total_mb is None or value.total_mb <= 0:
        return "N/A"
    return f"{value.used_mb / 1024:.1f} / {value.total_mb / 1024:.1f} GB"


def dashboard_title(hostname: str | None = None) -> str:
    """Return the cached local hostname as the compact dashboard title."""
    title = HOSTNAME if hostname is None else hostname
    return str(title).strip() or "UNKNOWN HOST"


def _latest_history_time(snapshot: DashboardSnapshot) -> float:
    timestamps = [
        sample.elapsed_s
        for sample in (*snapshot.cpu_history, *snapshot.gpu_history)
        if _finite_number(sample.elapsed_s) is not None
    ]
    return max(timestamps, default=0.0)


def _synthetic_preview_history(now_s: float, base_c: float, amplitude_c: float) -> tuple[TemperatureSample, ...]:
    """Create explicitly synthetic samples for offline preview rendering only."""
    return tuple(
        TemperatureSample(
            elapsed_s=now_s - remaining_s,
            value=base_c + amplitude_c * math.sin(remaining_s / 48.0),
        )
        for remaining_s in range(int(TEMPERATURE_WINDOW_S), -1, -1)
    )


def preview_snapshot(phase_s: float) -> DashboardSnapshot:
    """Attach synthetic curves only to the offline DEMO preview frame."""
    snapshot = demo_snapshot()
    return replace(
        snapshot,
        cpu_history=_synthetic_preview_history(phase_s, 58.0, 7.0),
        gpu_history=_synthetic_preview_history(phase_s, 63.0, 9.0),
        history_now_s=phase_s,
    )


def animation_phase(monotonic_s: float) -> BurnInPhase:
    """Return repeatable slow hue and bounded global placement from monotonic time."""
    elapsed = float(monotonic_s)
    if not math.isfinite(elapsed):
        raise DashboardError("animation time must be finite")
    hue = (30.0 + 360.0 * ((elapsed % HUE_CYCLE_S) / HUE_CYCLE_S)) % 360.0
    shift_x = round(SHIFT_LIMIT_PX * math.sin(math.tau * elapsed / 83.0))
    shift_y = round(SHIFT_LIMIT_PX * math.sin(math.tau * elapsed / 127.0 + math.pi / 2))
    return BurnInPhase(hue_degrees=hue, shift_x=shift_x, shift_y=shift_y)


def _hsl_hex(hue_degrees: float, saturation: float, lightness: float) -> str:
    red, green, blue = colorsys.hls_to_rgb(hue_degrees / 360.0, lightness, saturation)
    return f"#{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"


def palette_for_phase(phase: BurnInPhase) -> DashboardPalette:
    """Keep every visible foreground on the same slow, moderate-luminance hue cycle."""
    hue = phase.hue_degrees
    return DashboardPalette(
        accent=_hsl_hex(hue, 0.78, 0.52),
        text=_hsl_hex(hue, 0.48, 0.66),
        value=_hsl_hex(hue, 0.42, 0.70),
        border=_hsl_hex(hue, 0.52, 0.30),
        separator=_hsl_hex(hue, 0.43, 0.22),
        muted=_hsl_hex(hue, 0.38, 0.46),
    )


def _qt_bindings() -> tuple[Any, Any, Any, Any, Any, Any]:
    """Import Qt only for preview/render work; module import remains stdlib-only."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPen

    global _QT_APPLICATION
    existing = QGuiApplication.instance()
    _QT_APPLICATION = existing if existing is not None else QGuiApplication([])
    return QColor, QFont, QImage, QPainter, QPen, (QRect, Qt)


_QT_APPLICATION: Any = None


def _draw_text(
    painter: Any,
    qt: tuple[Any, Any],
    rect: tuple[int, int, int, int],
    text: str,
    *,
    color: str,
    pixels: int,
    bold: bool = False,
    align: Any | None = None,
) -> None:
    QColor, QFont, _QImage, _QPainter, _QPen, (QRect, Qt) = _qt_bindings()
    font = QFont("DejaVu Sans")
    font.setPixelSize(pixels)
    font.setBold(bold)
    painter.setFont(font)
    painter.setPen(QColor(color))
    alignment = align if align is not None else (
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )
    painter.drawText(QRect(*rect), alignment, text)


def elide_text(text: str, *, pixels: int, width: int, bold: bool = False) -> str:
    """Use Qt metrics so a long hostname cannot overlap the fixed live status."""
    _QColor, QFont, _QImage, _QPainter, _QPen, (_QRect, Qt) = _qt_bindings()
    from PySide6.QtGui import QFontMetrics

    font = QFont("DejaVu Sans")
    font.setPixelSize(pixels)
    font.setBold(bold)
    return QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, width)


def _draw_elided_text(
    painter: Any,
    qt: tuple[Any, Any],
    rect: tuple[int, int, int, int],
    text: str,
    *,
    color: str,
    pixels: int,
    bold: bool = False,
    align: Any | None = None,
) -> str:
    elided = elide_text(text, pixels=pixels, width=rect[2], bold=bold)
    _draw_text(painter, qt, rect, elided, color=color, pixels=pixels, bold=bold, align=align)
    return elided


def _draw_temperature_graph(
    painter: Any,
    qt: tuple[Any, Any],
    rect: tuple[int, int, int, int],
    samples: Sequence[TemperatureSample],
    now_s: float,
    palette: DashboardPalette,
) -> None:
    """Draw only measured runs on a black fixed-scale temperature graph."""
    QColor, _QFont, _QImage, _QPainter, QPen, (QRect, _Qt) = _qt_bindings()
    x, y, width, height = rect
    painter.fillRect(QRect(x, y, width, height), QColor(CARD_BACKGROUND))
    painter.setPen(QPen(QColor(palette.separator), 1))
    painter.drawRect(QRect(x, y, width, height))

    for segment in temperature_segments(samples, now_s=now_s):
        points = [
            (
                x + timestamp_to_x(sample.elapsed_s, now_s=now_s, width=width),
                *temperature_to_y(sample.value, top=y, height=height),
            )
            for sample in segment
        ]
        painter.setPen(QPen(QColor(palette.value), 2))
        for (left_x, left_y, _), (right_x, right_y, _) in zip(points, points[1:]):
            painter.drawLine(left_x, left_y, right_x, right_y)
        painter.setBrush(QColor(CARD_BACKGROUND))
        for point_x, point_y, out_of_range in points:
            point_color = palette.accent if out_of_range else palette.value
            painter.setPen(QPen(QColor(point_color), 2))
            painter.drawEllipse(point_x - 3, point_y - 3, 6, 6)
            if out_of_range:
                painter.drawLine(point_x - 4, point_y - 4, point_x + 4, point_y + 4)
                painter.drawLine(point_x - 4, point_y + 4, point_x + 4, point_y - 4)


def _draw_metric_card(
    painter: Any,
    qt: tuple[Any, Any],
    rect: tuple[int, int, int, int],
    title: str,
    metric: MetricCard,
    history: Sequence[TemperatureSample],
    history_now_s: float,
    palette: DashboardPalette,
) -> None:
    QColor, _QFont, _QImage, _QPainter, QPen, (QRect, Qt) = _qt_bindings()
    x, y, width, height = rect
    graph_x, graph_y, graph_width, graph_height = x + 396, y + 116, 250, 82
    painter.setBrush(QColor(CARD_BACKGROUND))
    painter.setPen(QPen(QColor(palette.border), 2))
    painter.drawRoundedRect(QRect(x, y, width, height), 28, 28)
    painter.setPen(QPen(QColor(palette.accent), 5))
    painter.drawLine(x + 32, y + 37, x + 118, y + 37)
    _draw_text(painter, qt, (x + 32, y + 53, width - 64, 38), title,
               color=palette.accent, pixels=25, bold=True)
    _draw_text(painter, qt, (x + 32, y + 102, 350, 112),
               format_temperature(metric.temp), color=palette.value, pixels=78, bold=True)
    _draw_text(painter, qt, (graph_x, y + 88, 120, 20), "0-110°C",
               color=palette.muted, pixels=14, bold=True)
    visible = prune_temperature_samples(history, now_s=history_now_s)
    if any(
        (value := _finite_number(sample.value)) is not None
        and not TEMPERATURE_MIN_C <= value <= TEMPERATURE_MAX_C
        for sample in visible
    ):
        _draw_text(painter, qt, (graph_x + 130, y + 88, graph_width - 130, 20), "! CLAMPED",
                   color=palette.accent, pixels=12, bold=True,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    _draw_temperature_graph(painter, qt, (graph_x, graph_y, graph_width, graph_height),
                            history, history_now_s, palette)
    _draw_text(painter, qt, (graph_x, y + 201, 80, 20), "-5m",
               color=palette.muted, pixels=14, bold=True)
    _draw_text(painter, qt, (graph_x + graph_width - 64, y + 201, 64, 20), "NOW",
               color=palette.muted, pixels=14, bold=True,
               align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    painter.setPen(QPen(QColor(palette.separator), 1))
    painter.drawLine(x + 34, y + 236, x + width - 34, y + 236)
    _draw_text(painter, qt, (x + 34, y + 252, 180, 34), "UTILIZATION",
               color=palette.muted, pixels=20, bold=True)
    _draw_text(painter, qt, (x + width - 210, y + 246, 170, 46),
               format_percent(metric.usage), color=palette.accent, pixels=36, bold=True,
               align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


def render_dashboard_image(
    snapshot: DashboardSnapshot, phase: BurnInPhase | None = None, *, hostname: str | None = None
) -> Any:
    """Render one black-card 1600x720 frame with a deterministic style-only phase."""
    QColor, _QFont, QImage, QPainter, QPen, (QRect, Qt) = _qt_bindings()
    phase = animation_phase(0.0) if phase is None else phase
    palette = palette_for_phase(phase)
    image = QImage(*FRAME_SIZE, QImage.Format.Format_ARGB32)
    image.fill(QColor(BACKGROUND))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # Base edges leave 6px travel inside the existing safe 72px outer margin.
    painter.translate(phase.shift_x, phase.shift_y)

    painter.setPen(QPen(QColor(palette.accent), 4))
    painter.drawLine(80, 79, 266, 79)
    _draw_elided_text(painter, (QRect, Qt), (80, 95, 880, 40), dashboard_title(hostname),
                      color=palette.text, pixels=28, bold=True)
    status = "DEMO • SYNTHETIC SNAPSHOT" if snapshot.demo else "LIVE • CACHED RAW SNAPSHOT"
    _draw_text(painter, (QRect, Qt), (1004, 95, 516, 40), status,
               color=palette.accent if snapshot.demo else palette.muted, pixels=20, bold=True,
               align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    history_now_s = snapshot.history_now_s
    if _finite_number(history_now_s) is None:
        history_now_s = _latest_history_time(snapshot)
    _draw_metric_card(painter, (QRect, Qt), (80, 164, 688, 318), "CPU / TCTL", snapshot.cpu,
                      snapshot.cpu_history, history_now_s, palette)
    _draw_metric_card(painter, (QRect, Qt), (832, 164, 688, 318), "GPU / PRIMARY", snapshot.gpu,
                      snapshot.gpu_history, history_now_s, palette)

    painter.setBrush(QColor(CARD_BACKGROUND))
    painter.setPen(QPen(QColor(palette.border), 2))
    painter.drawRoundedRect(QRect(80, 514, 1440, 128), 28, 28)
    painter.setPen(QPen(QColor(palette.accent), 5))
    painter.drawLine(114, 550, 200, 550)
    _draw_text(painter, (QRect, Qt), (114, 564, 330, 30), "RAM USAGE",
               color=palette.accent, pixels=24, bold=True)
    _draw_text(painter, (QRect, Qt), (470, 540, 640, 60), format_ram(snapshot.ram),
               color=palette.value, pixels=43, bold=True,
               align=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
    _draw_text(painter, (QRect, Qt), (1180, 542, 280, 56), format_percent(snapshot.ram.percent),
               color=palette.accent, pixels=43, bold=True,
               align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    _draw_text(painter, (QRect, Qt), (114, 600, 800, 24),
               "UNAVAILABLE VALUES ARE SHOWN AS N/A", color=palette.muted, pixels=16)
    painter.end()
    return image


def render_black_image() -> Any:
    """Render a 1600x720 frame containing no pixels other than true black."""
    QColor, _QFont, QImage, _QPainter, _QPen, _qt = _qt_bindings()
    image = QImage(*FRAME_SIZE, QImage.Format.Format_ARGB32)
    image.fill(QColor(BACKGROUND))
    return image


def render_black_png(output: Path) -> None:
    """Write the cached-while-standby black frame without dashboard artwork."""
    if not output.parent.is_dir():
        raise DashboardError(f"output directory does not exist: {output.parent}")
    if not render_black_image().save(str(output), "PNG"):
        raise DashboardError(f"failed to write PNG: {output}")


def render_dashboard_png(
    snapshot: DashboardSnapshot,
    output: Path,
    phase: BurnInPhase | None = None,
    *,
    hostname: str | None = None,
) -> None:
    """Render a fixed 1600x720 PNG at an already-existing temporary/output path."""
    if not output.parent.is_dir():
        raise DashboardError(f"output directory does not exist: {output.parent}")
    image = render_dashboard_image(snapshot, phase, hostname=hostname)
    if not image.save(str(output), "PNG"):
        raise DashboardError(f"failed to write PNG: {output}")


def write_preview(output: Path, phase_s: float = 0.0, *, hostname: str | None = None) -> None:
    """Write the explicitly synthetic offline preview; do not boot TRCC here."""
    render_dashboard_png(preview_snapshot(phase_s), output, animation_phase(phase_s), hostname=hostname)


def write_cinematic_preview(output: Path, phase_s: float = 0.0, *, hostname: str | None = None) -> None:
    """Write the cinematic renderer's synthetic DEMO preview without live dependencies."""
    cinematic = _load_sibling("thermalright_cinematic", "thermalright_cinematic.py")
    cinematic.write_cinematic_preview(output, time_s=phase_s, hostname=HOSTNAME if hostname is None else hostname)


def _cinematic_snapshot_with_histories(
    snapshot: DashboardSnapshot,
    cpu_history: TemperatureHistory,
    gpu_history: TemperatureHistory,
    elapsed_s: float,
) -> DashboardSnapshot:
    """Attach the existing bounded dashboard histories to one cinematic frame."""
    return replace(
        snapshot,
        demo=False,
        cpu_history=cpu_history.samples,
        gpu_history=gpu_history.samples,
        history_now_s=elapsed_s,
    )


def _positive_finite_duration(value: str) -> float:
    try:
        duration = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive finite number") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return duration


def _require_duration(duration_s: float) -> float:
    try:
        duration = float(duration_s)
    except (TypeError, ValueError) as exc:
        raise DashboardError("duration must be a positive finite number") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise DashboardError("duration must be a positive finite number")
    return duration


def validate_connected_profile(result: object) -> None:
    """Require the exact current connection handshake before any payload write."""
    if not bool(getattr(result, "ok", False)):
        raise DashboardError(f"connection failed: {getattr(result, 'message', 'unknown error')}")
    handshake = getattr(result, "handshake", None)
    if handshake is None:
        # Installed EnsureConnected returns a ConnectResult.  A no-op connection
        # has handshake=None, so it is not enough evidence to send this fixed-size
        # dashboard to a device whose current profile we cannot prove.
        raise DashboardError("connection did not return a handshake for profile validation")
    observed = (
        getattr(handshake, "pm_byte", None),
        getattr(handshake, "fbl", None),
        getattr(handshake, "resolution", None),
    )
    expected = (EXPECTED_PM_BYTE, EXPECTED_FBL, FRAME_SIZE)
    if observed != expected:
        raise DashboardError(
            "refusing image send: expected PM=64 FBL=114 resolution=1600x720, "
            f"got PM={observed[0]} FBL={observed[1]} resolution={observed[2]}"
        )


def _require_private_trcc_process() -> None:
    """Refuse the exact installed setting that selects TRCC's shared AppProxy."""
    if os.environ.get("TRCC_DAEMON") == "1":
        raise DashboardError(
            "TRCC_DAEMON=1 selects shared daemon mode; refusing to import or use it. "
            "Unset it or set TRCC_DAEMON=0 for this private dashboard."
        )


@contextmanager
def live_instance_lock() -> Iterator[None]:
    """Hold the per-user runtime lock for a live process, never a PID-file lock."""
    runtime_value = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_value:
        raise DashboardError("XDG_RUNTIME_DIR is required for the live instance lock")
    runtime_dir = Path(runtime_value)
    if not runtime_dir.is_dir():
        raise DashboardError(f"XDG_RUNTIME_DIR is not a directory: {runtime_dir}")
    try:
        import fcntl
    except ImportError as exc:
        raise DashboardError("live dashboard locking requires Linux fcntl") from exc

    try:
        descriptor = os.open(runtime_dir / LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise DashboardError(f"cannot open live instance lock: {exc}") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DashboardError("another Thermalright dashboard is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _default_dependencies() -> LiveDependencies:
    """Load TRCC only when the caller selected live mode."""
    from trcc._boot import trcc
    from trcc.core.commands import EnsureConnected, SendImage

    return LiveDependencies(
        app_factory=trcc,
        ensure_connected=lambda key: EnsureConnected(key=key),
        send_image=lambda key, path: SendImage(key=key, path=path),
        render_png=render_dashboard_png,
        render_black_png=render_black_png,
        read_dpms_state=KdeWaylandDpmsProvider().read_state,
    )


def _validate_encoder_path(value: Path | str) -> Path:
    """Load the optional stdlib client only for the explicit Rust encoder route."""
    jpeg = _load_sibling("thermalright_jpeg", "thermalright_jpeg.py")
    return jpeg.validate_encoder_path(value)


def _new_encoder_session(executable: Path) -> Any:
    jpeg = _load_sibling("thermalright_jpeg", "thermalright_jpeg.py")
    return jpeg.EncoderSession(executable, "420", JPEG_TIMEOUT_S)


def _rgba8888_payload(image: Any) -> bytes:
    """Copy one fixed RGBA8888 frame through the stdlib client after live guards."""
    from PySide6.QtGui import QImage

    jpeg = _load_sibling("thermalright_jpeg", "thermalright_jpeg.py")
    return jpeg.rgba8888_payload(image, rgba_format=QImage.Format.Format_RGBA8888)


def _rust_jpeg_saver(session: Any) -> Callable[[Any, Path], bool]:
    """Use the shared bounded writer while retaining a narrow live image adapter."""
    from PySide6.QtGui import QImage

    jpeg = _load_sibling("thermalright_jpeg", "thermalright_jpeg.py")
    return jpeg.rust_jpeg_saver(
        session,
        rgba_format=QImage.Format.Format_RGBA8888,
        payload_adapter=_rgba8888_payload,
    )


def _log_encoder_selection(path: Path) -> None:
    print(
        "cinematic-gpu: Rust JPEG encoder selected "
        f"({path}); parent process_time excludes encoder child CPU; child CPU requires cgroup accounting",
        file=sys.stderr,
        flush=True,
    )


def _send_succeeded(result: object) -> bool:
    return bool(getattr(result, "ok", False))


def _unknown_dpms_state() -> DpmsState:
    """Keep direct renderer tests and callers offline unless they inject a backend."""
    return DpmsState.UNKNOWN


def _read_dpms_state(reader: Callable[[], DpmsState]) -> DpmsState:
    """Treat an injected backend failure or unexpected value as UNKNOWN too."""
    try:
        state = reader()
    except Exception:
        return DpmsState.UNKNOWN
    return state if isinstance(state, DpmsState) else DpmsState.UNKNOWN


def _log_dpms_transition(
    previous: DpmsState | None, current: DpmsState, output_state: DpmsState
) -> None:
    """Warn once per unavailable transition while avoiding per-poll noise."""
    if current is previous:
        return
    if current is DpmsState.UNKNOWN:
        logging.warning("DPMS state unavailable; holding %s output", output_state.value)
    elif previous is DpmsState.UNKNOWN:
        logging.info("DPMS state recovered: %s", current.value)
    elif previous is not None:
        logging.info("DPMS state changed: %s", current.value)


def _send_frame(send: Callable[[Path], object], path: Path) -> None:
    result = send(path)
    if not _send_succeeded(result):
        raise DashboardError(f"image send failed: {getattr(result, 'message', 'unknown error')}")


def run_render_loop(
    *,
    duration_s: float | None,
    continuous: bool = False,
    read_snapshot: Callable[[], DashboardSnapshot],
    render_png: Callable[[DashboardSnapshot, Path, BurnInPhase], None],
    send: Callable[[Path], object],
    tempdir: Path,
    render_black_png: Callable[[Path], None] = render_black_png,
    read_dpms_state: Callable[[], DpmsState] = _unknown_dpms_state,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    stop_event: threading.Event | None = None,
) -> int:
    """Render at 1 Hz, but stream cached black while all KDE outputs are DPMS off."""
    deadline = None if continuous else clock() + _require_duration(duration_s)
    next_render = clock()
    next_dpms_poll = clock()
    frames = 0
    frame_path = tempdir / "frame.png"
    black_path = tempdir / "black.png"
    black_cached = False
    output_state = DpmsState.AWAKE
    previous_dpms_state: DpmsState | None = None
    cpu_history = TemperatureHistory()
    gpu_history = TemperatureHistory()

    while stop_event is None or not stop_event.is_set():
        now = clock()
        remaining = None if deadline is None else deadline - now
        if remaining is not None and remaining <= 0:
            break

        if now >= next_dpms_poll:
            observed_state = _read_dpms_state(read_dpms_state)
            _log_dpms_transition(previous_dpms_state, observed_state, output_state)
            previous_dpms_state = observed_state
            next_dpms_poll = clock() + DPMS_POLL_INTERVAL_S
            now = clock()
            remaining = None if deadline is None else deadline - now
            if remaining is not None and remaining <= 0:
                break

            if observed_state is DpmsState.SLEEP and output_state is not DpmsState.SLEEP:
                if not black_cached:
                    render_black_png(black_path)
                    black_cached = True
                _send_frame(send, black_path)
                frames += 1
                output_state = DpmsState.SLEEP
                continue
            if observed_state is DpmsState.AWAKE and output_state is DpmsState.SLEEP:
                # A missing sample creates a graph gap even for a short standby.
                cpu_history.record(now, None)
                gpu_history.record(now, None)
                output_state = DpmsState.AWAKE
                next_render = now

        if output_state is DpmsState.AWAKE and now >= next_render:
            snapshot = snapshot_with_temperature_history(
                read_snapshot(),
                elapsed_s=now,
                cpu_history=cpu_history,
                gpu_history=gpu_history,
            )
            render_png(snapshot, frame_path, animation_phase(now))
            _send_frame(send, frame_path)
            frames += 1
            # Schedule from the completed frame; never catch up with a burst.
            next_render = clock() + RENDER_INTERVAL_S
            continue

        next_wake = next_dpms_poll
        if output_state is DpmsState.AWAKE:
            next_wake = min(next_wake, next_render)
        if deadline is not None:
            next_wake = min(next_wake, deadline)
        # Keep signal response at the existing one-second cadence while asleep.
        wait_for = min(RENDER_INTERVAL_S, max(0.0, next_wake - clock()))
        if wait_for > 0:
            sleep(wait_for)

    return frames


def run_live(
    duration_s: float | None,
    *,
    continuous: bool = False,
    dependencies: LiveDependencies | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    stop_event: threading.Event | None = None,
    instance_lock: Callable[[], AbstractContextManager[None]] = live_instance_lock,
) -> int:
    """Boot one private TRCC app and close it after deadline, signal, or failure."""
    _require_private_trcc_process()
    with instance_lock():
        app: Any | None = None
        try:
            deps = dependencies if dependencies is not None else _default_dependencies()
            app = deps.app_factory()
            connection = app.dispatch(deps.ensure_connected(DEVICE_KEY))
            validate_connected_profile(connection)
            try:
                sensor_provider = RawTrccSensorProvider(app.platform.sensors())
            except Exception:
                sensor_provider = RawTrccSensorProvider(None)

            with tempfile.TemporaryDirectory(prefix="thermalright-dashboard-") as directory:
                return run_render_loop(
                    duration_s=duration_s,
                    continuous=continuous,
                    read_snapshot=lambda: snapshot_from_readings(sensor_provider.readings()),
                    render_png=deps.render_png,
                    render_black_png=deps.render_black_png,
                    send=lambda path: app.dispatch(deps.send_image(DEVICE_KEY, path)),
                    read_dpms_state=deps.read_dpms_state,
                    tempdir=Path(directory),
                    clock=clock,
                    sleep=sleep,
                    stop_event=stop_event,
                )
        finally:
            if app is not None:
                app.close()


def run_cinematic_live(
    duration_s: float | None,
    *,
    continuous: bool = False,
    renderer_backend: str = "cpu",
    jpeg_encoder: Path | str | None = None,
    dependencies: LiveDependencies | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    stop_event: threading.Event | None = None,
    instance_lock: Callable[[], AbstractContextManager[None]] = live_instance_lock,
) -> int:
    """Run the opt-in 24 Hz host-target cinematic renderer through private TRCC."""
    if renderer_backend not in ("cpu", "gpu"):
        raise DashboardError("cinematic renderer backend must be 'cpu' or 'gpu'")
    if jpeg_encoder is not None and renderer_backend != "gpu":
        raise DashboardError("--jpeg-encoder requires the cinematic-gpu renderer")
    if jpeg_encoder is not None and not continuous:
        raise DashboardError("--jpeg-encoder requires --continuous")
    _require_private_trcc_process()
    if renderer_backend == "gpu" and threading.current_thread() is not threading.main_thread():
        raise DashboardError("cinematic-gpu renderer must run on the main thread")
    encoder_path: Path | None = None
    if jpeg_encoder is not None:
        try:
            encoder_path = _validate_encoder_path(jpeg_encoder)
        except ValueError as error:
            raise DashboardError(str(error)) from error
    with instance_lock():
        app: Any | None = None
        gpu_sidecar: Any | None = None
        encoder_session: Any | None = None
        try:
            # Only selected cinematic live mode resolves these installed siblings.
            runtime = _load_sibling("thermalright_cinematic_runtime", "thermalright_cinematic_runtime.py")
            cinematic = _load_sibling("thermalright_cinematic", "thermalright_cinematic.py")
            if renderer_backend == "gpu":
                try:
                    gpu = _load_sibling("thermalright_cinematic_gpu", "thermalright_cinematic_gpu.py")
                    gpu_sidecar = gpu.GpuFluidSidecar()
                except Exception as error:
                    raise DashboardError(f"cinematic-gpu renderer unavailable: {error}") from error
            if encoder_path is not None:
                encoder_session = _new_encoder_session(encoder_path)
                _log_encoder_selection(encoder_path)
            deps = dependencies if dependencies is not None else _default_dependencies()
            app = deps.app_factory()
            connection = app.dispatch(deps.ensure_connected(DEVICE_KEY))
            validate_connected_profile(connection)
            try:
                sensor_provider = RawTrccSensorProvider(app.platform.sensors())
            except Exception:
                sensor_provider = RawTrccSensorProvider(None)

            support = runtime.CinematicRuntimeSupport(
                awake_state=DpmsState.AWAKE,
                sleep_state=DpmsState.SLEEP,
                read_dpms=_read_dpms_state,
                error_type=DashboardError,
                snapshot_from_readings=snapshot_from_readings,
                history_factory=TemperatureHistory,
                attach_histories=_cinematic_snapshot_with_histories,
            )
            with tempfile.TemporaryDirectory(prefix="thermalright-cinematic-") as directory:
                # Only the selected live route configures its private stderr INFO sink.
                logger = runtime.configure_production_logging()
                if gpu_sidecar is not None:
                    logger.info(
                        "cinematic-gpu renderer=hardware vendor=%s renderer=%s version=%s",
                        gpu_sidecar.info.vendor, gpu_sidecar.info.renderer, gpu_sidecar.info.version,
                    )
                # Production uses scalar aggregates only; the finite trial installs its own collector.
                metrics = runtime.ProductionMetricsLogger(started_at=clock())
                if gpu_sidecar is None:
                    render_image = lambda snapshot, elapsed_s: cinematic.render_cinematic_image(
                        snapshot, elapsed_s, hostname=HOSTNAME
                    )
                else:
                    render_image = lambda snapshot, elapsed_s: cinematic.render_cinematic_image(
                        snapshot, elapsed_s, hostname=HOSTNAME, field_renderer=gpu_sidecar.render
                    )
                save_jpeg = (
                    _rust_jpeg_saver(encoder_session)
                    if encoder_session is not None
                    else lambda image, path: bool(image.save(str(path), "JPEG", 85))
                )
                result = runtime.run_cinematic_loop(
                    duration_s=duration_s,
                    continuous=continuous,
                    dependencies=runtime.CinematicRuntimeDependencies(
                        read_dpms_state=deps.read_dpms_state,
                        render_image=render_image,
                        black_image=render_black_image,
                        save_jpeg=save_jpeg,
                        send_image=lambda path: app.dispatch(deps.send_image(DEVICE_KEY, path)),
                        save_black_jpeg=(
                            (lambda image, path: bool(image.save(str(path), "JPEG", 85)))
                            if encoder_session is not None
                            else None
                        ),
                    ),
                    support=support,
                    read_readings=sensor_provider.readings,
                    tempdir=Path(directory),
                    clock=clock,
                    sleep=sleep,
                    stop_event=stop_event,
                    on_frame=metrics.record,
                )
            return result.frames
        finally:
            try:
                if app is not None:
                    app.close()
            finally:
                try:
                    if encoder_session is not None:
                        encoder_session.close()
                finally:
                    if gpu_sidecar is not None:
                        gpu_sidecar.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    live_mode = parser.add_mutually_exclusive_group()
    live_mode.add_argument(
        "--duration",
        type=_positive_finite_duration,
        default=DEFAULT_DURATION_S,
        metavar="SECONDS",
        help="live foreground duration in seconds (default: 120)",
    )
    live_mode.add_argument(
        "--continuous",
        action="store_true",
        help="run the selected renderer until SIGINT, SIGTERM, or an image send failure",
    )
    parser.add_argument(
        "--renderer",
        choices=("classic", "cinematic", "cinematic-gpu"),
        default="classic",
        help="live renderer; --preview uses cinematic only for --renderer cinematic (default: classic)",
    )
    parser.add_argument(
        "--preview",
        type=Path,
        metavar="PATH",
        help="offline synthetic DEMO PNG; does not boot TRCC or access USB",
    )
    parser.add_argument(
        "--jpeg-encoder",
        metavar="ABSOLUTE_PATH",
        help="experimental persistent Rust JPEG encoder for continuous cinematic-gpu only",
    )
    args = parser.parse_args(argv)
    if args.jpeg_encoder is not None:
        if args.renderer != "cinematic-gpu":
            parser.error("--jpeg-encoder requires --renderer cinematic-gpu")
        if not args.continuous:
            parser.error("--jpeg-encoder requires --continuous")
        if args.preview is not None:
            parser.error("--jpeg-encoder cannot be used with --preview")
    return args


def _install_stop_handlers(stop_event: threading.Event) -> Callable[[], None]:
    previous: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    for signal_type in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signal_type] = signal.getsignal(signal_type)
            signal.signal(signal_type, request_stop)
        except (ValueError, OSError):
            # Signal handlers are unavailable outside the main thread/platform.
            continue

    def restore() -> None:
        for signal_type, handler in previous.items():
            signal.signal(signal_type, handler)

    return restore


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.preview is not None:
        try:
            writer = write_cinematic_preview if args.renderer == "cinematic" else write_preview
            writer(args.preview)
        except (DashboardError, ImportError, OSError, ValueError) as exc:
            print(f"preview failed: {exc}", file=sys.stderr)
            return 1
        print(f"wrote DEMO preview: {args.preview}")
        return 0

    stop_event = threading.Event()
    restore_handlers = _install_stop_handlers(stop_event)
    try:
        runner = run_cinematic_live if args.renderer in ("cinematic", "cinematic-gpu") else run_live
        runner_kwargs: dict[str, Any] = {"continuous": args.continuous, "stop_event": stop_event}
        if args.renderer == "cinematic-gpu":
            runner_kwargs["renderer_backend"] = "gpu"
            if args.jpeg_encoder is not None:
                runner_kwargs["jpeg_encoder"] = args.jpeg_encoder
        frames = runner(None if args.continuous else args.duration, **runner_kwargs)
    except (DashboardError, ImportError, OSError) as exc:
        print(f"dashboard failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"dashboard failed unexpectedly: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        restore_handlers()
    print(f"dashboard finished after {frames} frame(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
