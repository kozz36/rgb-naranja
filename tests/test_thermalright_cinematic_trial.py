"""Focused behavior tests for the isolated 24 FPS Thermalright cinematic trial."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import importlib.util
import math
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "thermalright-cinematic-trial.py"
SPEC = importlib.util.spec_from_file_location("thermalright_cinematic_trial", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
trial = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trial
SPEC.loader.exec_module(trial)
dashboard = trial.dashboard


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.cpu = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def process_time(self) -> float:
        return self.cpu

    def advance(self, seconds: float, *, cpu_seconds: float | None = None) -> None:
        self.now += seconds
        self.cpu += seconds if cpu_seconds is None else cpu_seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds, cpu_seconds=0.0)


class FakeEnsureConnected:
    def __init__(self, key: str) -> None:
        self.key = key


class FakeSendImage:
    def __init__(self, key: str, path: Path) -> None:
        self.key = key
        self.path = path


class FakeSensors:
    def __init__(self, clock: FakeClock, readings: object = None) -> None:
        self.clock = clock
        self.readings = {} if readings is None else readings
        self.times: list[float] = []

    def read_all(self) -> object:
        self.times.append(self.clock.now)
        if isinstance(self.readings, BaseException):
            raise self.readings
        return self.readings() if callable(self.readings) else self.readings


class FakePlatform:
    def __init__(self, sensors: FakeSensors) -> None:
        self._sensors = sensors

    def sensors(self) -> FakeSensors:
        return self._sensors


class FakeApp:
    def __init__(
        self,
        clock: FakeClock,
        *,
        connection: object | None = None,
        readings: object = None,
        send_results: list[object] | None = None,
    ) -> None:
        self.platform = FakePlatform(FakeSensors(clock, readings))
        self.connection = confirmed_connection() if connection is None else connection
        self.send_results = list(send_results or [SimpleNamespace(ok=True, bytes_sent=123, message="sent")])
        self.commands: list[object] = []
        self.sent_paths: list[Path] = []
        self.closed = False

    def dispatch(self, command: object) -> object:
        self.commands.append(command)
        if isinstance(command, FakeEnsureConnected):
            return self.connection
        if isinstance(command, FakeSendImage):
            self.sent_paths.append(command.path)
            if len(self.send_results) > 1:
                return self.send_results.pop(0)
            return self.send_results[0]
        raise AssertionError(f"unexpected command: {command!r}")

    def close(self) -> None:
        self.closed = True


def confirmed_connection(**overrides: object) -> SimpleNamespace:
    handshake = SimpleNamespace(pm_byte=64, fbl=114, resolution=(1600, 720))
    for name, value in overrides.items():
        setattr(handshake, name, value)
    return SimpleNamespace(ok=True, handshake=handshake, message="connected")


def dependencies_for(
    app: FakeApp,
    *,
    dpms=lambda: dashboard.DpmsState.UNKNOWN,
    render=None,
    save=None,
    black_save=None,
    black_image=None,
    app_factory=None,
) -> tuple[trial.TrialDependencies, list[object], list[tuple[object, Path]]]:
    rendered: list[object] = []
    saves: list[tuple[object, Path]] = []

    def render_image(snapshot: object, elapsed_s: float) -> object:
        rendered.append((snapshot, elapsed_s))
        return object()

    def save_jpeg(image: object, path: Path) -> bool:
        saves.append((image, path))
        path.write_bytes(b"jpeg-input")
        return True

    return (
        trial.TrialDependencies(
            app_factory=(lambda: app) if app_factory is None else app_factory,
            ensure_connected=lambda key: FakeEnsureConnected(key),
            send_image=lambda key, path: FakeSendImage(key, path),
            read_dpms_state=dpms,
            render_image=render_image if render is None else render,
            black_image=(lambda: "black-image") if black_image is None else black_image,
            save_jpeg=save_jpeg if save is None else save,
            save_black_jpeg=black_save,
        ),
        rendered,
        saves,
    )


def run_fake(
    app: FakeApp,
    clock: FakeClock,
    *,
    duration: object,
    dependencies: trial.TrialDependencies | None = None,
    stop_event: threading.Event | None = None,
    instance_lock=lambda: nullcontext(),
) -> tuple[trial.TrialSummary, list[dict[str, object]], list[object], list[tuple[object, Path]]]:
    if dependencies is None:
        dependencies, rendered, saves = dependencies_for(app)
    else:
        rendered, saves = [], []
    records: list[dict[str, object]] = []
    summary = trial.run_trial(
        duration,
        dependencies=dependencies,
        clock=clock.monotonic,
        sleep=clock.sleep,
        process_clock=clock.process_time,
        stop_event=stop_event,
        instance_lock=instance_lock,
        emit=records.append,
    )
    return summary, records, rendered, saves


class CliTests(unittest.TestCase):
    def test_default_duration_is_sixty_seconds_and_fps_is_fixed(self) -> None:
        self.assertEqual(trial.parse_args([]).duration, 60.0)
        self.assertEqual(trial.FPS, 24)
        self.assertEqual(trial.FRAME_INTERVAL_S, 1.0 / 24.0)
        with self.assertRaises(SystemExit):
            trial.parse_args(["--continuous"])

    def test_cli_duration_must_be_positive_finite_and_at_most_sixty(self) -> None:
        for invalid in ("0", "-1", "nan", "inf", "-inf", "60.1", "not-a-number"):
            with self.subTest(invalid=invalid), self.assertRaises(SystemExit):
                trial.parse_args(["--duration", invalid])

    def test_invalid_runtime_duration_rejects_before_dependencies_boot(self) -> None:
        clock = FakeClock()
        app = FakeApp(clock)
        factory_called = False

        def factory() -> FakeApp:
            nonlocal factory_called
            factory_called = True
            return app

        dependencies, _, _ = dependencies_for(app, app_factory=factory)
        for invalid in (0, -1, 60.1, math.nan, math.inf, -math.inf, True, False):
            with self.subTest(invalid=invalid), self.assertRaises(dashboard.DashboardError):
                trial.run_trial(
                    invalid,
                    dependencies=dependencies,
                    clock=clock.monotonic,
                    sleep=clock.sleep,
                    process_clock=clock.process_time,
                    instance_lock=lambda: nullcontext(),
                    emit=lambda _record: None,
                )
        self.assertFalse(factory_called)


class TimingTests(unittest.TestCase):
    def test_fast_work_keeps_frame_starts_near_twenty_four_hz_not_work_plus_sleep(self) -> None:
        clock = FakeClock()
        app = FakeApp(clock)
        starts: list[float] = []

        def render(snapshot: object, elapsed_s: float) -> object:
            starts.append(clock.now)
            clock.advance(0.010)
            return object()

        dependencies, _, _ = dependencies_for(app, render=render)
        summary, records, _, _ = run_fake(app, clock, duration=0.5, dependencies=dependencies)

        self.assertEqual(len(app.sent_paths), 12)
        self.assertEqual(len(starts), 12)
        self.assertTrue(all(abs((right - left) - 1.0 / 24.0) < 1e-9 for left, right in zip(starts, starts[1:])))
        self.assertAlmostEqual(summary.effective_awake_completed_send_rate_hz or 0.0, 24.0, places=6)
        self.assertEqual(len([record for record in records if record["type"] == "frame"]), 12)

    def test_slow_work_drops_frames_without_a_catchup_burst(self) -> None:
        clock = FakeClock()
        app = FakeApp(clock)
        starts: list[float] = []

        def render(snapshot: object, elapsed_s: float) -> object:
            starts.append(clock.now)
            clock.advance(0.100)
            return object()

        dependencies, _, _ = dependencies_for(app, render=render)
        run_fake(app, clock, duration=0.51, dependencies=dependencies)

        self.assertLess(len(app.sent_paths), 12)
        self.assertGreater(len(app.sent_paths), 0)
        self.assertTrue(all(right - left >= 0.099999 for left, right in zip(starts, starts[1:])))
        self.assertEqual(len(starts), len(set(starts)))

    def test_deadline_and_stop_event_prevent_new_sends_after_expensive_work(self) -> None:
        for mode in ("deadline", "signal"):
            with self.subTest(mode=mode):
                clock = FakeClock()
                app = FakeApp(clock)
                stop_event = threading.Event()

                def render(snapshot: object, elapsed_s: float) -> object:
                    if mode == "deadline":
                        clock.advance(0.11)
                    else:
                        stop_event.set()
                    return object()

                dependencies, _, _ = dependencies_for(app, render=render)
                summary, records, _, _ = run_fake(
                    app,
                    clock,
                    duration=0.1,
                    dependencies=dependencies,
                    stop_event=stop_event,
                )
                self.assertEqual(app.sent_paths, [])
                self.assertEqual(summary.awake_frames, 0)
                self.assertEqual([record["type"] for record in records], ["summary"])

    def test_stop_after_one_sleep_allows_no_extra_send(self) -> None:
        clock = FakeClock()
        app = FakeApp(clock)
        stop_event = threading.Event()
        dependencies, _, _ = dependencies_for(app)

        def stop_sleep(seconds: float) -> None:
            clock.sleep(seconds)
            stop_event.set()

        records: list[dict[str, object]] = []
        summary = trial.run_trial(
            1.0,
            dependencies=dependencies,
            clock=clock.monotonic,
            sleep=stop_sleep,
            process_clock=clock.process_time,
            stop_event=stop_event,
            instance_lock=lambda: nullcontext(),
            emit=records.append,
        )

        self.assertEqual(summary.awake_frames, 1)
        self.assertEqual(len(app.sent_paths), 1)
        self.assertEqual(len([record for record in records if record["type"] == "frame"]), 1)


class SensorAndDpmsTests(unittest.TestCase):
    def test_sensors_poll_no_more_than_once_every_two_seconds_and_snapshots_are_truthful(self) -> None:
        clock = FakeClock()
        app = FakeApp(clock, readings={})
        dependencies, rendered, _ = dependencies_for(app)
        run_fake(app, clock, duration=4.1, dependencies=dependencies)

        self.assertEqual(len(app.platform._sensors.times), 3)
        self.assertEqual(app.platform._sensors.times[0], 0.0)
        self.assertTrue(all(right - left >= 2.0 for left, right in zip(app.platform._sensors.times, app.platform._sensors.times[1:])))
        first_snapshot, _first_elapsed = rendered[0]
        self.assertFalse(first_snapshot.demo)
        self.assertIsNone(first_snapshot.cpu.temp)
        self.assertIsNone(first_snapshot.gpu.temp)
        self.assertIsNone(first_snapshot.ram.used_mb)
        final_snapshot, _final_elapsed = rendered[-1]
        self.assertEqual(
            [sample.elapsed_s for sample in final_snapshot.cpu_history], app.platform._sensors.times
        )
        self.assertGreaterEqual(final_snapshot.history_now_s, app.platform._sensors.times[-1])

    def test_confirmed_sleep_uses_one_cached_black_jpeg_at_one_hz_without_sensors_or_animation(self) -> None:
        clock = FakeClock()
        app = FakeApp(clock)
        black_images: list[object] = []
        states = iter((dashboard.DpmsState.SLEEP, dashboard.DpmsState.SLEEP))

        def black_image() -> object:
            image = object()
            black_images.append(image)
            return image

        rust_saves: list[tuple[object, Path]] = []
        qt_black_saves: list[tuple[object, Path]] = []

        def rust_save(image: object, path: Path) -> bool:
            rust_saves.append((image, path))
            return False

        def qt_black_save(image: object, path: Path) -> bool:
            qt_black_saves.append((image, path))
            path.write_bytes(b"qt-black")
            return True

        dependencies, rendered, _ = dependencies_for(
            app,
            dpms=lambda: next(states),
            save=rust_save,
            black_save=qt_black_save,
            black_image=black_image,
            render=lambda _snapshot, _elapsed: self.fail("sleep must not animate"),
        )
        summary, records, _, _ = run_fake(app, clock, duration=2.1, dependencies=dependencies)

        self.assertEqual(summary.awake_frames, 0)
        self.assertEqual(summary.black_frames, 3)
        self.assertEqual(app.platform._sensors.times, [])
        self.assertEqual(rendered, [])
        self.assertEqual(len(black_images), 1)
        self.assertEqual(rust_saves, [])
        self.assertEqual(len(qt_black_saves), 1)
        self.assertTrue(all(path.name == "black.jpeg" for path in app.sent_paths))
        self.assertEqual([record["state"] for record in records if record["type"] == "frame"], ["black", "black", "black"])

    def test_unknown_holds_initial_awake_and_holds_black_after_confirmed_sleep(self) -> None:
        clock = FakeClock()
        app = FakeApp(clock)
        states = iter((dashboard.DpmsState.UNKNOWN, dashboard.DpmsState.SLEEP, dashboard.DpmsState.UNKNOWN))
        dependencies, _, _ = dependencies_for(app, dpms=lambda: next(states))
        summary, records, _, _ = run_fake(app, clock, duration=4.1, dependencies=dependencies)

        states_sent = [record["state"] for record in records if record["type"] == "frame"]
        self.assertGreater(summary.awake_frames, 0)
        self.assertGreater(summary.black_frames, 0)
        self.assertEqual(states_sent[0], "awake")
        self.assertEqual(states_sent[-1], "black")
        self.assertTrue(all(path.name == "black.jpeg" for path in app.sent_paths[-3:]))

    def test_wake_inserts_history_gap_and_renders_immediately(self) -> None:
        clock = FakeClock()
        app = FakeApp(clock, readings={"cpu:temp": 51.0, "gpu:primary:temp": 61.0})
        states = iter((dashboard.DpmsState.AWAKE, dashboard.DpmsState.SLEEP, dashboard.DpmsState.AWAKE))
        dependencies, rendered, _ = dependencies_for(app, dpms=lambda: next(states))
        run_fake(app, clock, duration=4.1, dependencies=dependencies)

        wake_frames = [snapshot for snapshot, elapsed in rendered if elapsed >= 4.0]
        self.assertTrue(wake_frames)
        history = wake_frames[0].cpu_history
        self.assertIn(None, [sample.value for sample in history])
        gap_index = [sample.value for sample in history].index(None)
        self.assertEqual(history[gap_index].elapsed_s, 4.0)
        self.assertGreaterEqual(app.platform._sensors.times[-1] - app.platform._sensors.times[0], 2.0)


class LiveBoundaryTests(unittest.TestCase):
    def test_invalid_handshake_sends_nothing_and_closes_while_lock_is_held(self) -> None:
        clock = FakeClock()
        app = FakeApp(clock, connection=confirmed_connection(pm_byte=63))
        dependencies, _, _ = dependencies_for(app)
        lock_held = False

        @contextmanager
        def held_lock():
            nonlocal lock_held
            lock_held = True
            try:
                yield
            finally:
                lock_held = False

        original_close = app.close

        def checked_close() -> None:
            self.assertTrue(lock_held)
            original_close()

        app.close = checked_close  # type: ignore[method-assign]
        with self.assertRaises(dashboard.DashboardError):
            run_fake(app, clock, duration=0.1, dependencies=dependencies, instance_lock=held_lock)

        self.assertTrue(app.closed)
        self.assertEqual(app.sent_paths, [])

    def test_daemon_mode_refuses_before_default_dependencies_or_app_factory(self) -> None:
        clock = FakeClock()
        app = FakeApp(clock)
        factory_called = False

        def factory() -> FakeApp:
            nonlocal factory_called
            factory_called = True
            return app

        dependencies, _, _ = dependencies_for(app, app_factory=factory)
        with patch.dict(os.environ, {"TRCC_DAEMON": "1"}, clear=False), patch.object(
            trial, "_default_dependencies", side_effect=AssertionError("TRCC must not boot")
        ) as default_dependencies, self.assertRaisesRegex(dashboard.DashboardError, "TRCC_DAEMON=1"):
            run_fake(app, clock, duration=0.1, dependencies=dependencies)

        self.assertFalse(factory_called)
        default_dependencies.assert_not_called()
        self.assertFalse(app.closed)

    def test_send_failure_closes_app_and_keeps_lock_through_close(self) -> None:
        clock = FakeClock()
        app = FakeApp(clock, send_results=[SimpleNamespace(ok=False, bytes_sent=0, message="synthetic failure")])
        dependencies, _, _ = dependencies_for(app)
        lock_held = False

        @contextmanager
        def held_lock():
            nonlocal lock_held
            lock_held = True
            try:
                yield
            finally:
                lock_held = False

        original_close = app.close

        def checked_close() -> None:
            self.assertTrue(lock_held)
            original_close()

        app.close = checked_close  # type: ignore[method-assign]
        with self.assertRaisesRegex(dashboard.DashboardError, "image send failed"):
            run_fake(app, clock, duration=0.1, dependencies=dependencies, instance_lock=held_lock)

        self.assertTrue(app.closed)
        self.assertEqual(len(app.sent_paths), 1)

    def test_native_frame_bytes_is_optional_and_jpeg_input_bytes_is_observed_file_length(self) -> None:
        clock = FakeClock()
        invalid = [
            SimpleNamespace(ok=True, message="missing"),
            SimpleNamespace(ok=True, bytes_sent=True, message="bool"),
            SimpleNamespace(ok=True, bytes_sent=-1, message="negative"),
            SimpleNamespace(ok=True, bytes_sent="123", message="text"),
            SimpleNamespace(ok=True, bytes_sent=456, message="valid"),
        ]
        app = FakeApp(clock, send_results=invalid)
        dependencies, _, _ = dependencies_for(app)
        _summary, records, _, _ = run_fake(app, clock, duration=0.21, dependencies=dependencies)

        frame_records = [record for record in records if record["type"] == "frame"]
        native_bytes = [record["native_frame_bytes"] for record in frame_records]
        self.assertEqual(native_bytes[:5], [None, None, None, None, 456])
        self.assertTrue(all(value == 456 for value in native_bytes[5:]))
        self.assertEqual({record["jpeg_input_bytes"] for record in frame_records}, {10})
        self.assertTrue(all("physical_panel_fps" not in record for record in frame_records))
        self.assertEqual(records[-1]["physical_panel_fps"], None)

    def test_jpeg_save_failure_sends_nothing_and_closes(self) -> None:
        clock = FakeClock()
        app = FakeApp(clock)

        def save_error(_image: object, _path: Path) -> bool:
            raise RuntimeError("Rust encoder protocol timeout")

        dependencies, _, _ = dependencies_for(app, save=save_error)
        with self.assertRaisesRegex(RuntimeError, "Rust encoder protocol timeout"):
            run_fake(app, clock, duration=0.1, dependencies=dependencies)

        self.assertEqual(app.sent_paths, [])
        self.assertTrue(app.closed)


if __name__ == "__main__":
    unittest.main()
