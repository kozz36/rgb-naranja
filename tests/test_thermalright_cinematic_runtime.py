"""Focused behavior tests for the shared cinematic runtime."""

from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
import importlib.util
import io
import logging
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(module_name: str, filename: str):
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


runtime = _load("thermalright_cinematic_runtime", "thermalright_cinematic_runtime.py")
dashboard = _load("thermalright_dashboard", "thermalright-dashboard.py")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class CinematicRuntimeTests(unittest.TestCase):
    def test_save_black_jpeg_is_optional_for_existing_runtime_callers(self) -> None:
        dependencies = runtime.CinematicRuntimeDependencies(
            read_dpms_state=lambda: dashboard.DpmsState.AWAKE,
            render_image=lambda _snapshot, _elapsed: object(),
            black_image=lambda: object(),
            save_jpeg=lambda _image, _path: True,
            send_image=lambda _path: SimpleNamespace(ok=True),
        )
        self.assertIsNone(dependencies.save_black_jpeg)

    def test_default_metrics_sink_is_visible_with_root_warning_without_duplicate_handlers(self) -> None:
        """The production route owns one stderr INFO sink without changing root logging."""
        root = logging.getLogger()
        original_root_level = root.level
        captured = io.StringIO()
        frame = runtime.CinematicFrame(
            sequence=1,
            state="awake",
            timestamp_s=60.0,
            render_wall_s=0.010,
            render_cpu_s=None,
            jpeg_wall_s=0.020,
            jpeg_cpu_s=None,
            send_wall_s=0.030,
            send_cpu_s=None,
            jpeg_input_bytes=4,
            native_frame_bytes=4,
            completed_at=60.0,
            awake_elapsed_s=60.0,
        )
        try:
            root.setLevel(logging.WARNING)
            with redirect_stderr(captured):
                configured = runtime.configure_production_logging()
                self.assertIs(configured, runtime.configure_production_logging())
                metrics = runtime.ProductionMetricsLogger(started_at=0.0)
                metrics.record(frame)
            self.assertEqual(root.level, logging.WARNING)
            self.assertIn("INFO:rgb_naranja.thermalright.cinematic:cinematic aggregate", captured.getvalue())
            self.assertEqual(captured.getvalue().count("cinematic aggregate"), 1)
            owned = [
                handler
                for handler in configured.handlers
                if getattr(handler, "_rgb_naranja_cinematic_owned", False)
            ]
            self.assertEqual(len(owned), 1)
        finally:
            root.setLevel(original_root_level)
            runtime.configure_production_logging()

    def test_continuous_ten_thousand_frames_keeps_production_metrics_bounded(self) -> None:
        """Continuous production telemetry has scalar aggregates, never a frame collector."""
        clock = FakeClock()
        stop_event = threading.Event()
        sent = 0
        logs: list[str] = []

        def send_image(_path: Path) -> object:
            nonlocal sent
            sent += 1
            if sent == 10_000:
                stop_event.set()
            return SimpleNamespace(ok=True, bytes_sent=123)

        support = runtime.CinematicRuntimeSupport(
            awake_state=dashboard.DpmsState.AWAKE,
            sleep_state=dashboard.DpmsState.SLEEP,
            read_dpms=dashboard._read_dpms_state,
            error_type=dashboard.DashboardError,
            snapshot_from_readings=dashboard.snapshot_from_readings,
            history_factory=dashboard.TemperatureHistory,
            attach_histories=lambda snapshot, cpu, gpu, elapsed: replace(
                snapshot,
                demo=False,
                cpu_history=cpu.samples,
                gpu_history=gpu.samples,
                history_now_s=elapsed,
            ),
        )
        dependencies = runtime.CinematicRuntimeDependencies(
            read_dpms_state=lambda: dashboard.DpmsState.AWAKE,
            render_image=lambda _snapshot, _elapsed: object(),
            black_image=lambda: self.fail("awake output must not render black"),
            save_jpeg=lambda _image, path: path.write_bytes(b"jpeg") is not None,
            send_image=send_image,
        )
        logger = runtime.ProductionMetricsLogger(started_at=clock.monotonic(), log_info=logs.append)

        with tempfile.TemporaryDirectory() as directory:
            result = runtime.run_cinematic_loop(
                duration_s=None,
                continuous=True,
                dependencies=dependencies,
                support=support,
                read_readings=lambda: {},
                tempdir=Path(directory),
                clock=clock.monotonic,
                sleep=clock.sleep,
                stop_event=stop_event,
                on_frame=logger.record,
            )

        self.assertEqual(result.awake_frames, 10_000)
        self.assertEqual(result.black_frames, 0)
        self.assertEqual(sent, 10_000)
        self.assertEqual(logger.lifetime_frames, 10_000)
        self.assertEqual(logger.buffered_frame_count, 0)
        self.assertEqual(len(logs), 6)
        self.assertTrue(all("cinematic aggregate" in message for message in logs))
        self.assertTrue(all(wait > 0 for wait in clock.sleeps))


if __name__ == "__main__":
    unittest.main()
