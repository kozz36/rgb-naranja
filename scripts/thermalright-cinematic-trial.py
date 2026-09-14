#!/usr/bin/env python3
"""Run one private, finite 24 FPS cinematic physical-panel trial.

This temporary entrypoint is for the user-authorized 60-second physical trial only
*after* visual approval of the 24 FPS WebM.  It neither starts nor controls a
service, USB rule, sensor daemon, DPMS state, or system power state.  A parent
watchdog owns app boot and handshake bounds; the trial deadline begins after the
validated private app is ready and includes every later loop operation.
"""
from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
from typing import Any, Callable

# Set before either sibling can lazily import Qt.  An explicit caller setting wins.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEVICE_KEY = "87ad:70db"
FPS = 24
FRAME_INTERVAL_S = 1.0 / FPS
BLACK_KEEPALIVE_INTERVAL_S = 1.0
POLL_INTERVAL_S = 2.0
DEFAULT_DURATION_S = 60.0
MAX_DURATION_S = 60.0


def _load_sibling(module_name: str, filename: str) -> Any:
    """Import a sibling from this installed script directory exactly once."""
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_DIRECTORY / filename)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling module {filename}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules during class creation.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


dashboard = _load_sibling("thermalright_dashboard", "thermalright-dashboard.py")
cinematic = _load_sibling("thermalright_cinematic", "thermalright_cinematic.py")
runtime = _load_sibling("thermalright_cinematic_runtime", "thermalright_cinematic_runtime.py")


@dataclass(frozen=True)
class TrialDependencies:
    """Injectable live boundaries; all renderer and transport calls stay synchronous."""

    app_factory: Callable[[], Any]
    ensure_connected: Callable[[str], Any]
    send_image: Callable[[str, Path], Any]
    read_dpms_state: Callable[[], Any]
    render_image: Callable[[Any, float], Any]
    black_image: Callable[[], Any]
    save_jpeg: Callable[[Any, Path], bool]
    save_black_jpeg: Callable[[Any, Path], bool] | None = None


@dataclass(frozen=True)
class TrialSummary:
    """Host-side observations, not a claim about physical panel refresh."""

    awake_frames: int
    black_frames: int
    elapsed_wall_s: float
    parent_cpu_s: float
    awake_elapsed_wall_s: float
    effective_awake_completed_send_rate_hz: float | None
    render: dict[str, float | None]
    jpeg: dict[str, float | None]
    send: dict[str, float | None]

    def as_json(self) -> dict[str, Any]:
        return {
            "type": "summary",
            "awake_frames": self.awake_frames,
            "black_frames": self.black_frames,
            "elapsed_wall_s": self.elapsed_wall_s,
            "parent_cpu_s": self.parent_cpu_s,
            "awake_elapsed_wall_s": self.awake_elapsed_wall_s,
            "effective_awake_completed_send_rate_hz": self.effective_awake_completed_send_rate_hz,
            "effective_awake_completed_send_rate_definition": (
                "successful awake SendImage frames divided by elapsed wall time while the "
                "trial output state was awake; excludes confirmed DPMS-sleep intervals "
                "and includes host render/JPEG/send work"
            ),
            "render_s": self.render,
            "jpeg_s": self.jpeg,
            "send_s": self.send,
            "physical_panel_fps": None,
        }


def _render_live_image(
    snapshot: Any, elapsed_s: float, *, field_renderer: Callable[[Any, float], Any] | None = None
) -> Any:
    return cinematic.render_cinematic_image(
        snapshot, elapsed_s, hostname=dashboard.HOSTNAME, field_renderer=field_renderer
    )


def _new_black_image() -> Any:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    image = QImage(*dashboard.FRAME_SIZE, QImage.Format.Format_RGB888)
    image.fill(Qt.GlobalColor.black)
    return image


def _save_jpeg(image: Any, path: Path) -> bool:
    return bool(image.save(str(path), "JPEG", 85))


def _default_dependencies() -> TrialDependencies:
    """Reuse the dashboard's lazy native TRCC boundary exactly once."""
    live = dashboard._default_dependencies()
    return TrialDependencies(
        app_factory=live.app_factory,
        ensure_connected=live.ensure_connected,
        send_image=live.send_image,
        read_dpms_state=live.read_dpms_state,
        render_image=_render_live_image,
        black_image=_new_black_image,
        save_jpeg=_save_jpeg,
        save_black_jpeg=_save_jpeg,
    )


def _duration(value: object) -> float:
    if isinstance(value, bool):
        raise dashboard.DashboardError("duration must be a positive finite number at most 60 seconds")
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise dashboard.DashboardError("duration must be a positive finite number at most 60 seconds") from exc
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_DURATION_S:
        raise dashboard.DashboardError("duration must be a positive finite number at most 60 seconds")
    return duration


def _duration_argument(value: str) -> float:
    try:
        return _duration(value)
    except dashboard.DashboardError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _clock_value(clock: Callable[[], float]) -> float:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise dashboard.DashboardError("clock must return a finite number")
    return float(value)


def _elapsed(start: float, end: float) -> float:
    value = end - start
    return value if math.isfinite(value) and value >= 0 else 0.0


def _read_dpms(reader: Callable[[], Any]) -> Any:
    try:
        state = reader()
    except Exception:
        return dashboard.DpmsState.UNKNOWN
    return state if isinstance(state, dashboard.DpmsState) else dashboard.DpmsState.UNKNOWN


def _valid_native_frame_bytes(result: object) -> int | None:
    value = getattr(result, "bytes_sent", None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _distribution(samples: list[float]) -> dict[str, float | None]:
    if not samples:
        return {"median": None, "p95": None}
    ordered = sorted(samples)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0
    return {"median": median, "p95": ordered[math.ceil(len(ordered) * 0.95) - 1]}


def _emit_json(record: dict[str, Any]) -> None:
    print(json.dumps(record, allow_nan=False, sort_keys=True), flush=True)


def _image_file_length(path: Path) -> int:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise dashboard.DashboardError(f"JPEG output is unavailable: {path}") from exc
    if size < 0:
        raise dashboard.DashboardError(f"JPEG output length is invalid: {path}")
    return size


def _snapshot_with_histories(snapshot: Any, cpu_history: Any, gpu_history: Any, elapsed_s: float) -> Any:
    """Keep the trial's existing immutable frame shape while runtime owns cadence."""
    return replace(
        snapshot,
        demo=False,
        cpu_history=cpu_history.samples,
        gpu_history=gpu_history.samples,
        history_now_s=elapsed_s,
    )


def _is_stopped(stop_event: threading.Event | None, now: float, deadline: float) -> bool:
    return (stop_event is not None and stop_event.is_set()) or now >= deadline


def _runtime_support() -> Any:
    """Pass dashboard data/DPMS helpers without creating a reverse runtime import."""
    return runtime.CinematicRuntimeSupport(
        awake_state=dashboard.DpmsState.AWAKE,
        sleep_state=dashboard.DpmsState.SLEEP,
        read_dpms=dashboard._read_dpms_state,
        error_type=dashboard.DashboardError,
        snapshot_from_readings=dashboard.snapshot_from_readings,
        history_factory=dashboard.TemperatureHistory,
        attach_histories=_snapshot_with_histories,
    )


class _TrialCollector:
    """Finite-only frame JSON and percentile input retained by the 60-second trial."""

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self._emit = emit
        self.render_samples: list[float] = []
        self.jpeg_samples: list[float] = []
        self.send_samples: list[float] = []

    def record_measurement(self, measurement: Any) -> None:
        if measurement.phase == "render":
            self.render_samples.append(measurement.wall_s)
        elif measurement.phase == "jpeg":
            self.jpeg_samples.append(measurement.wall_s)
        elif measurement.phase == "send":
            self.send_samples.append(measurement.wall_s)

    def record_frame(self, frame: Any) -> None:
        self._emit(
            {
                "type": "frame",
                "sequence": frame.sequence,
                "state": frame.state,
                "timestamp_s": frame.timestamp_s,
                "render_wall_s": frame.render_wall_s,
                "render_cpu_s": frame.render_cpu_s,
                "jpeg_wall_s": frame.jpeg_wall_s,
                "jpeg_cpu_s": frame.jpeg_cpu_s,
                "send_wall_s": frame.send_wall_s,
                "send_cpu_s": frame.send_cpu_s,
                "jpeg_input_bytes": frame.jpeg_input_bytes,
                # Native SendResult.bytes_sent: frame bytes, not USB-wire bandwidth.
                "native_frame_bytes": frame.native_frame_bytes,
            }
        )


def run_trial(
    duration_s: object = DEFAULT_DURATION_S,
    *,
    dependencies: TrialDependencies | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    process_clock: Callable[[], float] = time.process_time,
    stop_event: threading.Event | None = None,
    instance_lock: Callable[[], AbstractContextManager[None]] = dashboard.live_instance_lock,
    emit: Callable[[dict[str, Any]], None] = _emit_json,
) -> TrialSummary:
    """Run a finite private trial; every native SendImage dispatch is synchronous."""
    duration = _duration(duration_s)  # Reject invalid input before booting any dependency.
    dashboard._require_private_trcc_process()

    with instance_lock():
        app: Any | None = None
        try:
            deps = dependencies if dependencies is not None else _default_dependencies()
            app = deps.app_factory()
            connection = app.dispatch(deps.ensure_connected(DEVICE_KEY))
            dashboard.validate_connected_profile(connection)
            try:
                sensor_provider = dashboard.RawTrccSensorProvider(app.platform.sensors())
            except Exception:
                sensor_provider = dashboard.RawTrccSensorProvider(None)

            with tempfile.TemporaryDirectory(prefix="thermalright-cinematic-trial-") as directory:
                return _run_loop(
                    duration=duration,
                    app=app,
                    dependencies=deps,
                    sensor_provider=sensor_provider,
                    tempdir=Path(directory),
                    clock=clock,
                    sleep=sleep,
                    process_clock=process_clock,
                    stop_event=stop_event,
                    emit=emit,
                )
        finally:
            if app is not None:
                app.close()


def _run_loop(
    *,
    duration: float,
    app: Any,
    dependencies: TrialDependencies,
    sensor_provider: Any,
    tempdir: Path,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    process_clock: Callable[[], float],
    stop_event: threading.Event | None,
    emit: Callable[[dict[str, Any]], None],
) -> TrialSummary:
    """Adapt the shared loop to the trial's finite JSON/percentile collector."""
    collector = _TrialCollector(emit)
    result = runtime.run_cinematic_loop(
        duration_s=duration,
        continuous=False,
        dependencies=runtime.CinematicRuntimeDependencies(
            read_dpms_state=dependencies.read_dpms_state,
            render_image=dependencies.render_image,
            black_image=dependencies.black_image,
            save_jpeg=dependencies.save_jpeg,
            send_image=lambda path: app.dispatch(dependencies.send_image(DEVICE_KEY, path)),
            save_black_jpeg=dependencies.save_black_jpeg,
        ),
        support=_runtime_support(),
        read_readings=sensor_provider.readings,
        tempdir=tempdir,
        clock=clock,
        sleep=sleep,
        process_clock=process_clock,
        stop_event=stop_event,
        on_measurement=collector.record_measurement,
        on_frame=collector.record_frame,
    )
    summary = TrialSummary(
        awake_frames=result.awake_frames,
        black_frames=result.black_frames,
        elapsed_wall_s=result.elapsed_wall_s,
        parent_cpu_s=0.0 if result.parent_cpu_s is None else result.parent_cpu_s,
        awake_elapsed_wall_s=result.awake_elapsed_wall_s,
        effective_awake_completed_send_rate_hz=(
            result.awake_frames / result.awake_elapsed_wall_s if result.awake_elapsed_wall_s > 0 else None
        ),
        render=_distribution(collector.render_samples),
        jpeg=_distribution(collector.jpeg_samples),
        send=_distribution(collector.send_samples),
    )
    emit(summary.as_json())
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--duration",
        type=_duration_argument,
        default=DEFAULT_DURATION_S,
        metavar="SECONDS",
        help="finite physical-trial duration in seconds, greater than 0 and at most 60 (default: 60)",
    )
    return parser.parse_args(argv)


def _install_stop_handlers(stop_event: threading.Event) -> Callable[[], None]:
    previous: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    for signal_type in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
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
    stop_event = threading.Event()
    restore_handlers = _install_stop_handlers(stop_event)
    try:
        run_trial(args.duration, stop_event=stop_event)
    except (dashboard.DashboardError, ImportError, OSError, ValueError) as exc:
        print(f"cinematic trial failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"cinematic trial failed unexpectedly: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        restore_handlers()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
