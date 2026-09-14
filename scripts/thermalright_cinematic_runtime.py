#!/usr/bin/env python3
"""Shared, dependency-free 24 Hz cinematic panel runtime.

This module intentionally knows nothing about TRCC, Qt, or either entrypoint.  The
entrypoints pass the dashboard's already-guarded live dependencies and data helpers
at the boundary.  It holds only the bounded temperature histories required by the
renderer; frame events are delivered synchronously and are never accumulated here.

The target is a host-side frame-start cadence, not a claim about physical panel FPS.
Each SendImage call is synchronous.  A soft deadline or signal prevents another send,
but a send already in flight is allowed to return so the caller can close its app and
release its lock cleanly.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import math
from pathlib import Path
import sys
import threading
import time
from typing import Any


FPS = 24
FRAME_INTERVAL_S = 1.0 / FPS
BLACK_KEEPALIVE_INTERVAL_S = 1.0
POLL_INTERVAL_S = 2.0
PRODUCTION_LOG_INTERVAL_S = 60.0
PRODUCTION_LOGGER_NAME = "rgb_naranja.thermalright.cinematic"
_OWNED_HANDLER_ATTRIBUTE = "_rgb_naranja_cinematic_owned"
_PRODUCTION_LOGGER = logging.getLogger(PRODUCTION_LOGGER_NAME)


def configure_production_logging() -> logging.Logger:
    """Attach one private stderr INFO handler without changing root logging state.

    The function is deliberately called only by the selected live cinematic route.
    Importing this module, previews, and the classic dashboard leave global logging
    untouched.  Reconfiguration reuses the owned handler and refreshes its stream,
    which prevents duplicate records across repeated in-process live sessions.
    """
    logger = _PRODUCTION_LOGGER
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = next(
        (
            candidate
            for candidate in logger.handlers
            if getattr(candidate, _OWNED_HANDLER_ATTRIBUTE, False)
        ),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler()
        setattr(handler, _OWNED_HANDLER_ATTRIBUTE, True)
        logger.addHandler(handler)
    elif handler.stream is not sys.stderr:
        handler.setStream(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    return logger


@dataclass(frozen=True)
class CinematicRuntimeSupport:
    """Dashboard-owned domain helpers injected without importing the dashboard."""

    awake_state: object
    sleep_state: object
    read_dpms: Callable[[Callable[[], object]], object]
    error_type: Callable[[str], Exception]
    snapshot_from_readings: Callable[[object], Any]
    history_factory: Callable[[], Any]
    attach_histories: Callable[[Any, Any, Any, float], Any]


@dataclass(frozen=True)
class CinematicRuntimeDependencies:
    """Synchronous renderer, storage, display state, and transport boundaries."""

    read_dpms_state: Callable[[], object]
    render_image: Callable[[Any, float], Any]
    black_image: Callable[[], Any]
    save_jpeg: Callable[[Any, Path], bool]
    send_image: Callable[[Path], object]
    save_black_jpeg: Callable[[Any, Path], bool] | None = None


@dataclass(frozen=True)
class CinematicMeasurement:
    """One completed host operation, delivered only to an optional observer."""

    phase: str
    wall_s: float
    cpu_s: float | None


@dataclass(frozen=True)
class CinematicFrame:
    """One successfully completed synchronous image send."""

    sequence: int
    state: str
    timestamp_s: float
    render_wall_s: float | None
    render_cpu_s: float | None
    jpeg_wall_s: float | None
    jpeg_cpu_s: float | None
    send_wall_s: float | None
    send_cpu_s: float | None
    jpeg_input_bytes: int | None
    native_frame_bytes: int | None
    completed_at: float
    awake_elapsed_s: float


@dataclass(frozen=True)
class CinematicRunResult:
    """Scalar completion facts; this object never retains individual frame data."""

    awake_frames: int
    black_frames: int
    elapsed_wall_s: float
    parent_cpu_s: float | None
    awake_elapsed_wall_s: float

    @property
    def frames(self) -> int:
        return self.awake_frames + self.black_frames


@dataclass
class _TimingAggregate:
    """Constant-space aggregate for one timing category."""

    count: int = 0
    total_s: float = 0.0
    max_s: float = 0.0

    def record(self, value: float | None) -> None:
        if value is None:
            return
        self.count += 1
        self.total_s += value
        self.max_s = max(self.max_s, value)

    @property
    def mean_s(self) -> float | None:
        return self.total_s / self.count if self.count else None


class ProductionMetricsLogger:
    """Emit 60-second scalar host aggregates without retaining per-frame samples."""

    def __init__(
        self,
        *,
        started_at: float,
        interval_s: float = PRODUCTION_LOG_INTERVAL_S,
        log_info: Callable[[str], None] | None = None,
    ) -> None:
        if not math.isfinite(started_at):
            raise ValueError("started_at must be finite")
        if not math.isfinite(interval_s) or interval_s <= 0:
            raise ValueError("interval_s must be finite and positive")
        self._interval_s = interval_s
        self._next_log_at = started_at + interval_s
        # Bind the owned logger at construction time, after the live route configured it.
        self._log_info = _PRODUCTION_LOGGER.info if log_info is None else log_info
        self._lifetime_frames = 0
        self._lifetime_awake_frames = 0
        self._lifetime_black_frames = 0
        self._interval_awake_elapsed_origin = 0.0
        self._reset_interval()

    @property
    def lifetime_frames(self) -> int:
        return self._lifetime_frames

    @property
    def lifetime_awake_frames(self) -> int:
        return self._lifetime_awake_frames

    @property
    def lifetime_black_frames(self) -> int:
        return self._lifetime_black_frames

    @property
    def buffered_frame_count(self) -> int:
        """Make the no-frame-buffer production contract observable to focused tests."""
        return 0

    def _reset_interval(self) -> None:
        self._interval_frames = 0
        self._interval_awake_frames = 0
        self._interval_black_frames = 0
        self._render = _TimingAggregate()
        self._jpeg = _TimingAggregate()
        self._send = _TimingAggregate()

    @staticmethod
    def _timing_text(name: str, values: _TimingAggregate) -> str:
        mean = "n/a" if values.mean_s is None else f"{values.mean_s:.6f}"
        maximum = "n/a" if not values.count else f"{values.max_s:.6f}"
        return f"{name}_count={values.count} {name}_mean_s={mean} {name}_max_s={maximum}"

    def record(self, frame: CinematicFrame) -> None:
        """Record a completed frame and log at most one aggregate per elapsed interval."""
        self._lifetime_frames += 1
        self._interval_frames += 1
        if frame.state == "awake":
            self._lifetime_awake_frames += 1
            self._interval_awake_frames += 1
        else:
            self._lifetime_black_frames += 1
            self._interval_black_frames += 1
        self._render.record(frame.render_wall_s)
        self._jpeg.record(frame.jpeg_wall_s)
        self._send.record(frame.send_wall_s)

        if frame.completed_at < self._next_log_at:
            return

        awake_elapsed = max(0.0, frame.awake_elapsed_s - self._interval_awake_elapsed_origin)
        awake_rate = self._interval_awake_frames / awake_elapsed if awake_elapsed > 0 else None
        rate_text = "n/a" if awake_rate is None else f"{awake_rate:.3f}"
        self._log_info(
            "cinematic aggregate "
            f"frames={self._interval_frames} awake_frames={self._interval_awake_frames} "
            f"black_frames={self._interval_black_frames} awake_host_rate_hz={rate_text} "
            f"{self._timing_text('render', self._render)} "
            f"{self._timing_text('jpeg', self._jpeg)} "
            f"{self._timing_text('send', self._send)}"
        )
        # A long synchronous send emits one newest aggregate, never a timer catch-up burst.
        self._next_log_at = frame.completed_at + self._interval_s
        self._interval_awake_elapsed_origin = frame.awake_elapsed_s
        self._reset_interval()


def _clock_value(clock: Callable[[], float], support: CinematicRuntimeSupport) -> float:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise support.error_type("clock must return a finite number")
    return float(value)


def _elapsed(start: float, end: float) -> float:
    value = end - start
    return value if math.isfinite(value) and value >= 0 else 0.0


def _require_duration(duration_s: object, support: CinematicRuntimeSupport) -> float:
    if isinstance(duration_s, bool):
        raise support.error_type("duration must be a positive finite number")
    try:
        duration = float(duration_s)
    except (TypeError, ValueError) as exc:
        raise support.error_type("duration must be a positive finite number") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise support.error_type("duration must be a positive finite number")
    return duration


def _is_stopped(stop_event: threading.Event | None, now: float, deadline: float | None) -> bool:
    return (stop_event is not None and stop_event.is_set()) or (deadline is not None and now >= deadline)


def _image_file_length(path: Path, support: CinematicRuntimeSupport) -> int:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise support.error_type(f"JPEG output is unavailable: {path}") from exc
    if size < 0:
        raise support.error_type(f"JPEG output length is invalid: {path}")
    return size


def _valid_native_frame_bytes(result: object) -> int | None:
    value = getattr(result, "bytes_sent", None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def run_cinematic_loop(
    *,
    duration_s: float | None,
    continuous: bool,
    dependencies: CinematicRuntimeDependencies,
    support: CinematicRuntimeSupport,
    read_readings: Callable[[], object],
    tempdir: Path,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    process_clock: Callable[[], float] | None = None,
    stop_event: threading.Event | None = None,
    on_measurement: Callable[[CinematicMeasurement], None] | None = None,
    on_frame: Callable[[CinematicFrame], None] | None = None,
) -> CinematicRunResult:
    """Run a bounded or signal-stopped cinematic loop with no internal frame collector.

    Awake frame cadence is scheduled from the start of each frame.  When synchronous
    work overruns the 1/24 second target, the next start is reset from that newest
    start, deliberately dropping stale frames instead of catching up.  DPMS and
    sensor polls are scheduled from completion, which prevents a slow backend from
    causing a polling burst.
    """
    duration = None if continuous else _require_duration(duration_s, support)
    started = _clock_value(clock, support)
    parent_cpu_started = _clock_value(process_clock, support) if process_clock is not None else None
    deadline = None if duration is None else started + duration
    next_frame = started
    next_dpms_poll = started
    next_sensor_poll = started
    output_state = support.awake_state  # UNKNOWN conservatively holds this initial output.
    output_state_started = started
    awake_elapsed = 0.0
    cpu_history = support.history_factory()
    gpu_history = support.history_factory()
    latest_snapshot = support.snapshot_from_readings({})
    black_path = tempdir / "black.jpeg"
    frame_path = tempdir / "frame.jpeg"
    black_cached = False
    black_jpeg_bytes: int | None = None
    sequence = 0
    awake_frames = 0
    black_frames = 0
    observe_timings = on_measurement is not None or on_frame is not None

    def awake_elapsed_at(at: float) -> float:
        if output_state is support.awake_state:
            return awake_elapsed + _elapsed(output_state_started, at)
        return awake_elapsed

    def change_output_state(new_state: object, at: float) -> None:
        nonlocal awake_elapsed, output_state, output_state_started
        if new_state is output_state:
            return
        if output_state is support.awake_state:
            awake_elapsed += _elapsed(output_state_started, at)
        output_state = new_state
        output_state_started = at

    def measure(phase: str, operation: Callable[[], Any]) -> tuple[Any, float | None, float | None, float]:
        if not observe_timings:
            value = operation()
            return value, None, None, _clock_value(clock, support)
        wall_started = _clock_value(clock, support)
        cpu_started = _clock_value(process_clock, support) if process_clock is not None else None
        value = operation()
        completed = _clock_value(clock, support)
        cpu_completed = _clock_value(process_clock, support) if process_clock is not None else None
        wall_s = _elapsed(wall_started, completed)
        cpu_s = _elapsed(cpu_started, cpu_completed) if cpu_started is not None and cpu_completed is not None else None
        if on_measurement is not None:
            on_measurement(CinematicMeasurement(phase=phase, wall_s=wall_s, cpu_s=cpu_s))
        return value, wall_s, cpu_s, completed

    while True:
        now = _clock_value(clock, support)
        if _is_stopped(stop_event, now, deadline):
            break

        if now >= next_dpms_poll:
            observed = support.read_dpms(dependencies.read_dpms_state)
            poll_completed = _clock_value(clock, support)
            next_dpms_poll = poll_completed + POLL_INTERVAL_S
            if observed is support.sleep_state and output_state is not support.sleep_state:
                change_output_state(support.sleep_state, poll_completed)
                next_frame = poll_completed
            elif observed is support.awake_state and output_state is support.sleep_state:
                wake_elapsed = _elapsed(started, poll_completed)
                cpu_history.record(wake_elapsed, None)
                gpu_history.record(wake_elapsed, None)
                change_output_state(support.awake_state, poll_completed)
                next_frame = poll_completed
            # UNKNOWN intentionally leaves the confirmed output and cadence unchanged.
            now = _clock_value(clock, support)
            if _is_stopped(stop_event, now, deadline):
                break

        if now >= next_frame:
            frame_started = _clock_value(clock, support)
            if _is_stopped(stop_event, frame_started, deadline):
                break
            state = "awake" if output_state is support.awake_state else "black"
            interval = FRAME_INTERVAL_S if state == "awake" else BLACK_KEEPALIVE_INTERVAL_S
            # Frame starts establish cadence.  Overruns reset from this newest start.
            next_frame = frame_started + interval
            render_wall_s: float | None = None
            render_cpu_s: float | None = None
            jpeg_wall_s: float | None = None
            jpeg_cpu_s: float | None = None
            jpeg_input_bytes: int | None = None

            if state == "awake":
                if frame_started >= next_sensor_poll:
                    readings = read_readings()
                    sensor_completed = _clock_value(clock, support)
                    latest_snapshot = support.snapshot_from_readings(readings)
                    sensor_elapsed = _elapsed(started, sensor_completed)
                    cpu_history.record(sensor_elapsed, latest_snapshot.cpu.temp)
                    gpu_history.record(sensor_elapsed, latest_snapshot.gpu.temp)
                    next_sensor_poll = sensor_completed + POLL_INTERVAL_S

                snapshot_elapsed = _elapsed(started, _clock_value(clock, support))
                snapshot = support.attach_histories(latest_snapshot, cpu_history, gpu_history, snapshot_elapsed)
                image, render_wall_s, render_cpu_s, _render_completed = measure(
                    "render", lambda: dependencies.render_image(snapshot, _elapsed(started, frame_started))
                )
                if _is_stopped(stop_event, _clock_value(clock, support), deadline):
                    break

                saved, jpeg_wall_s, jpeg_cpu_s, _jpeg_completed = measure(
                    "jpeg", lambda: dependencies.save_jpeg(image, frame_path)
                )
                if not saved:
                    raise support.error_type(f"failed to write JPEG: {frame_path}")
                jpeg_input_bytes = _image_file_length(frame_path, support)
                path = frame_path
            else:
                if not black_cached:
                    saved, jpeg_wall_s, jpeg_cpu_s, _jpeg_completed = measure(
                        "jpeg", lambda: (dependencies.save_black_jpeg or dependencies.save_jpeg)(
                            dependencies.black_image(), black_path
                        )
                    )
                    if not saved:
                        raise support.error_type(f"failed to write cached black JPEG: {black_path}")
                    black_jpeg_bytes = _image_file_length(black_path, support)
                    black_cached = True
                # Cached black keepalives reuse their first true-black JPEG without animation or sensors.
                jpeg_input_bytes = black_jpeg_bytes
                path = black_path

            # Soft stop/deadline checks prevent a new synchronous send after expensive work.
            if _is_stopped(stop_event, _clock_value(clock, support), deadline):
                break
            result, send_wall_s, send_cpu_s, send_completed = measure(
                "send", lambda: dependencies.send_image(path)
            )
            if not bool(getattr(result, "ok", False)):
                raise support.error_type(f"image send failed: {getattr(result, 'message', 'unknown error')}")

            sequence += 1
            if state == "awake":
                awake_frames += 1
            else:
                black_frames += 1
            if on_frame is not None:
                on_frame(
                    CinematicFrame(
                        sequence=sequence,
                        state=state,
                        timestamp_s=_elapsed(started, frame_started),
                        render_wall_s=render_wall_s,
                        render_cpu_s=render_cpu_s,
                        jpeg_wall_s=jpeg_wall_s,
                        jpeg_cpu_s=jpeg_cpu_s,
                        send_wall_s=send_wall_s,
                        send_cpu_s=send_cpu_s,
                        jpeg_input_bytes=jpeg_input_bytes,
                        native_frame_bytes=_valid_native_frame_bytes(result),
                        completed_at=send_completed,
                        awake_elapsed_s=awake_elapsed_at(send_completed),
                    )
                )
            continue

        next_wake = min(next_frame, next_dpms_poll)
        if deadline is not None:
            next_wake = min(next_wake, deadline)
        wait_for = max(0.0, next_wake - _clock_value(clock, support))
        if wait_for > 0:
            sleep(wait_for)

    finished = _clock_value(clock, support)
    if output_state is support.awake_state:
        awake_elapsed += _elapsed(output_state_started, finished)
    parent_cpu_s = (
        _elapsed(parent_cpu_started, _clock_value(process_clock, support))
        if parent_cpu_started is not None and process_clock is not None
        else None
    )
    return CinematicRunResult(
        awake_frames=awake_frames,
        black_frames=black_frames,
        elapsed_wall_s=_elapsed(started, finished),
        parent_cpu_s=parent_cpu_s,
        awake_elapsed_wall_s=awake_elapsed,
    )
