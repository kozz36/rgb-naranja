"""Focused behavior tests for the temporary Thermalright dashboard."""

from __future__ import annotations

from contextlib import nullcontext
import importlib.util
import inspect
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "thermalright-dashboard.py"
SPEC = importlib.util.spec_from_file_location("thermalright_dashboard", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeEnsureConnected:
    def __init__(self, *, key: str) -> None:
        self.key = key


class FakeSendImage:
    def __init__(self, *, key: str, path: Path) -> None:
        self.key = key
        self.path = path


class FakeSensors:
    def __init__(self, readings: object) -> None:
        self.readings = readings
        self.discover_called = False

    def read_all(self) -> object:
        if isinstance(self.readings, BaseException):
            raise self.readings
        return self.readings

    def discover(self) -> object:
        self.discover_called = True
        raise AssertionError("dashboard must not use discover()")


class FakePlatform:
    def __init__(self, sensors: FakeSensors) -> None:
        self._sensors = sensors

    def sensors(self) -> FakeSensors:
        return self._sensors


class FakeApp:
    def __init__(self, connect_result: object, readings: object, send_ok: bool = True) -> None:
        self.connect_result = connect_result
        self.platform = FakePlatform(FakeSensors(readings))
        self.send_ok = send_ok
        self.commands: list[object] = []
        self.closed = False

    def dispatch(self, command: object) -> object:
        self.commands.append(command)
        if isinstance(command, FakeEnsureConnected):
            return self.connect_result
        if isinstance(command, FakeSendImage):
            return SimpleNamespace(ok=self.send_ok, message="fake send")
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
    rendered: list[dashboard.DashboardSnapshot],
    *,
    read_dpms_state=lambda: dashboard.DpmsState.UNKNOWN,
    black_rendered: list[Path] | None = None,
) -> dashboard.LiveDependencies:
    black = [] if black_rendered is None else black_rendered
    return dashboard.LiveDependencies(
        app_factory=lambda: app,
        ensure_connected=lambda key: FakeEnsureConnected(key=key),
        send_image=lambda key, path: FakeSendImage(key=key, path=path),
        render_png=lambda snapshot, _path, _phase=None: rendered.append(snapshot),
        render_black_png=lambda _path: black.append(_path),
        read_dpms_state=read_dpms_state,
    )


class SnapshotTests(unittest.TestCase):
    def test_missing_and_nonfinite_readings_are_unavailable_not_zero(self) -> None:
        snapshot = dashboard.snapshot_from_readings(
            {
                "cpu:temp": float("nan"),
                "cpu:usage": float("inf"),
                "gpu:primary:temp": None,
                "gpu:primary:usage": "not-a-number",
                "memory:used": -float("inf"),
                "memory:total": None,
                "memory:percent": math.nan,
            }
        )

        self.assertEqual(snapshot.cpu.temp, None)
        self.assertEqual(snapshot.cpu.usage, None)
        self.assertEqual(snapshot.gpu.temp, None)
        self.assertEqual(snapshot.gpu.usage, None)
        self.assertEqual(snapshot.ram.used_mb, None)
        self.assertEqual(snapshot.ram.total_mb, None)
        self.assertEqual(snapshot.ram.percent, None)
        self.assertEqual(dashboard.format_temperature(snapshot.cpu.temp), "N/A")
        self.assertEqual(dashboard.format_percent(snapshot.gpu.usage), "N/A")

    def test_gpu_only_uses_raw_primary_alias(self) -> None:
        snapshot = dashboard.snapshot_from_readings(
            {"gpu:0:temp": 61.0, "gpu:0:usage": 40.0}
        )

        self.assertIsNone(snapshot.gpu.temp)
        self.assertIsNone(snapshot.gpu.usage)

    def test_live_sensor_provider_uses_read_all_not_zero_filled_discover(self) -> None:
        sensors = FakeSensors({"cpu:temp": 52.5})
        provider = dashboard.RawTrccSensorProvider(sensors)

        self.assertEqual(provider.readings(), {"cpu:temp": 52.5})
        self.assertFalse(sensors.discover_called)


class KdeDpmsTests(unittest.TestCase):
    def test_parser_requires_complete_known_nonduplicate_output_records(self) -> None:
        cases = {
            "dpms mode for screen HDMI-A-1: off\ndpms mode for screen DP-1: off\n": dashboard.DpmsState.SLEEP,
            "dpms mode for screen HDMI-A-1: on\ndpms mode for screen DP-1: off\n": dashboard.DpmsState.AWAKE,
            "dpms mode for screen HDMI-A-1: on\n": dashboard.DpmsState.AWAKE,
            "": dashboard.DpmsState.UNKNOWN,
            "\n": dashboard.DpmsState.UNKNOWN,
            "dpms mode for screen HDMI-A-1: standby\n": dashboard.DpmsState.UNKNOWN,
            "dpms mode for screen HDMI-A-1: off\nwarning: incomplete output\n": dashboard.DpmsState.UNKNOWN,
            "dpms mode for screen HDMI-A-1: off\ndpms mode for screen HDMI-A-1: off\n": dashboard.DpmsState.UNKNOWN,
        }
        for stdout, expected in cases.items():
            with self.subTest(stdout=stdout):
                self.assertIs(dashboard.parse_kscreen_dpms_stdout(stdout), expected)

    def test_provider_uses_c_locale_wayland_child_environment_and_exact_argv(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="dpms mode for screen HDMI-A-1: on\n")
        with patch.dict(
            os.environ,
            {"LC_ALL": "caller-lc", "LANG": "caller-lang", "QT_QPA_PLATFORM": "offscreen"},
            clear=False,
        ), patch.object(dashboard.subprocess, "run", return_value=completed) as run:
            state = dashboard.KdeWaylandDpmsProvider().read_state()
            self.assertEqual(os.environ["LC_ALL"], "caller-lc")
            self.assertEqual(os.environ["LANG"], "caller-lang")
            self.assertEqual(os.environ["QT_QPA_PLATFORM"], "offscreen")

        self.assertIs(state, dashboard.DpmsState.AWAKE)
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/bin/kscreen-doctor", "--dpms", "show"])
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["timeout"], dashboard.DPMS_QUERY_TIMEOUT_S)
        self.assertEqual(kwargs["env"]["LC_ALL"], "C.UTF-8")
        self.assertEqual(kwargs["env"]["LANG"], "C.UTF-8")
        self.assertEqual(kwargs["env"]["QT_QPA_PLATFORM"], "wayland")

    def test_provider_maps_timeout_missing_binary_and_nonzero_to_unknown(self) -> None:
        failures = (
            subprocess.TimeoutExpired(["/usr/bin/kscreen-doctor", "--dpms", "show"], 1.5),
            FileNotFoundError("kscreen-doctor missing"),
            SimpleNamespace(returncode=1, stdout="dpms mode for screen HDMI-A-1: off\n"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch.object(
                dashboard.subprocess,
                "run",
                side_effect=failure if isinstance(failure, BaseException) else None,
                return_value=None if isinstance(failure, BaseException) else failure,
            ):
                self.assertIs(
                    dashboard.KdeWaylandDpmsProvider().read_state(), dashboard.DpmsState.UNKNOWN
                )


class PowerSnapshotTests(unittest.TestCase):
    def test_raw_power_keys_are_separate_truthful_nonnegative_watts(self) -> None:
        snapshot = dashboard.snapshot_from_readings({
            "cpu:power": 75.33,
            "gpu:primary:power": 30.73,
            "gpu:0:power": 999.0,
            "cpu:temp": 58.0,
            "gpu:primary:temp": 63.0,
        })
        self.assertEqual(snapshot.cpu.power_w, 75.33)
        self.assertEqual(snapshot.gpu.power_w, 30.73)
        self.assertEqual(snapshot.cpu.temp, 58.0)
        self.assertEqual(snapshot.gpu.temp, 63.0)

        for invalid in (None, -0.1, float("nan"), float("inf"), -float("inf"), True, False):
            with self.subTest(invalid=invalid):
                mapped = dashboard.snapshot_from_readings({
                    "cpu:power": invalid,
                    "gpu:primary:power": invalid,
                    "gpu:0:power": 999.0,
                })
                self.assertIsNone(mapped.cpu.power_w)
                self.assertIsNone(mapped.gpu.power_w)
        zero = dashboard.snapshot_from_readings({"cpu:power": 0.0, "gpu:primary:power": 0})
        self.assertEqual(zero.cpu.power_w, 0.0)
        self.assertEqual(zero.gpu.power_w, 0.0)

    def test_power_format_and_demo_values_remain_explicitly_synthetic(self) -> None:
        self.assertEqual(dashboard.format_power(0.0), "0 W")
        self.assertEqual(dashboard.format_power(30.73), "31 W")
        self.assertEqual(dashboard.format_power(10_000.0), "10000 W")
        self.assertEqual(dashboard.format_power(None), "N/A")
        demo = dashboard.demo_snapshot()
        self.assertTrue(demo.demo)
        self.assertEqual((demo.cpu.power_w, demo.gpu.power_w), (75.0, 31.0))


class LoopTests(unittest.TestCase):
    def test_loop_is_bounded_to_one_render_per_second(self) -> None:
        clock = FakeClock()
        renders: list[dashboard.DashboardSnapshot] = []
        sends: list[Path] = []

        frames = dashboard.run_render_loop(
            duration_s=2.2,
            read_snapshot=lambda: dashboard.snapshot_from_readings({}),
            render_png=lambda snapshot, path, phase: renders.append((snapshot, phase)),
            send=lambda path: sends.append(path) or SimpleNamespace(ok=True),
            tempdir=Path("/tmp/dashboard-test"),
            clock=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(frames, 3)
        self.assertEqual(len(renders), 3)
        self.assertEqual(len(sends), 3)
        self.assertEqual(
            [phase for _snapshot, phase in renders],
            [dashboard.animation_phase(second) for second in (0.0, 1.0, 2.0)],
        )
        self.assertEqual(clock.sleeps, [1.0, 1.0, 0.20000000000000018])

    def test_dpms_polling_is_throttled_and_deadline_waits_without_busy_loop(self) -> None:
        clock = FakeClock()
        polls: list[float] = []
        renders: list[dashboard.DashboardSnapshot] = []

        def read_dpms_state() -> dashboard.DpmsState:
            polls.append(clock.now)
            return dashboard.DpmsState.AWAKE

        frames = dashboard.run_render_loop(
            duration_s=2.1,
            read_snapshot=dashboard.demo_snapshot,
            render_png=lambda snapshot, _path, _phase: renders.append(snapshot),
            render_black_png=lambda _path: self.fail("awake state must not render black"),
            send=lambda _path: SimpleNamespace(ok=True),
            read_dpms_state=read_dpms_state,
            tempdir=Path("/tmp/dashboard-test"),
            clock=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(frames, 3)
        self.assertEqual(polls, [0.0, 2.0])
        self.assertEqual(len(renders), 3)
        self.assertEqual(clock.sleeps[:2], [1.0, 1.0])
        self.assertAlmostEqual(clock.sleeps[-1], 0.1)

    def test_awake_sleep_awake_sends_one_black_skips_sensors_and_marks_graph_gap(self) -> None:
        clock = FakeClock()
        states = iter((dashboard.DpmsState.AWAKE, dashboard.DpmsState.SLEEP, dashboard.DpmsState.AWAKE))
        sensor_reads: list[float] = []
        normal_frames: list[dashboard.DashboardSnapshot] = []
        black_frames: list[Path] = []
        sent: list[str] = []

        def read_snapshot() -> dashboard.DashboardSnapshot:
            value = 50.0 + len(sensor_reads)
            sensor_reads.append(clock.now)
            return dashboard.snapshot_from_readings({"cpu:temp": value, "gpu:primary:temp": value + 10})

        frames = dashboard.run_render_loop(
            duration_s=4.1,
            read_snapshot=read_snapshot,
            render_png=lambda snapshot, _path, _phase: normal_frames.append(snapshot),
            render_black_png=lambda path: black_frames.append(path),
            send=lambda path: sent.append(path.name) or SimpleNamespace(ok=True),
            read_dpms_state=lambda: next(states),
            tempdir=Path("/tmp/dashboard-test"),
            clock=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(frames, 4)
        self.assertEqual(sensor_reads, [0.0, 1.0, 4.0])
        self.assertEqual(sent, ["frame.png", "frame.png", "black.png", "frame.png"])
        self.assertEqual(len(black_frames), 1)
        self.assertEqual(
            [sample.value for sample in normal_frames[-1].cpu_history], [50.0, 51.0, None, 52.0]
        )

    def test_startup_all_off_sends_black_before_any_dashboard_or_sensor_read(self) -> None:
        clock = FakeClock()
        black_frames: list[Path] = []
        sent: list[str] = []

        frames = dashboard.run_render_loop(
            duration_s=0.1,
            read_snapshot=lambda: self.fail("standby must skip sensor reads"),
            render_png=lambda _snapshot, _path, _phase: self.fail("standby must not flash dashboard"),
            render_black_png=lambda path: black_frames.append(path),
            send=lambda path: sent.append(path.name) or SimpleNamespace(ok=True),
            read_dpms_state=lambda: dashboard.DpmsState.SLEEP,
            tempdir=Path("/tmp/dashboard-test"),
            clock=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(frames, 1)
        self.assertEqual(sent, ["black.png"])
        self.assertEqual(len(black_frames), 1)
        self.assertEqual(clock.sleeps, [0.1])

    def test_unknown_holds_dashboard_initially_and_black_after_confirmed_sleep(self) -> None:
        clock = FakeClock()
        states = iter((
            dashboard.DpmsState.AWAKE,
            dashboard.DpmsState.UNKNOWN,
            dashboard.DpmsState.SLEEP,
            dashboard.DpmsState.UNKNOWN,
        ))
        normal_frames: list[dashboard.DashboardSnapshot] = []
        black_frames: list[Path] = []

        dashboard.run_render_loop(
            duration_s=6.1,
            read_snapshot=dashboard.demo_snapshot,
            render_png=lambda snapshot, _path, _phase: normal_frames.append(snapshot),
            render_black_png=lambda path: black_frames.append(path),
            send=lambda _path: SimpleNamespace(ok=True),
            read_dpms_state=lambda: next(states),
            tempdir=Path("/tmp/dashboard-test"),
            clock=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(len(normal_frames), 4)
        self.assertEqual(len(black_frames), 1)

    def test_continuous_loop_stops_on_signal_without_a_busy_loop(self) -> None:
        clock = FakeClock()
        stop_event = dashboard.threading.Event()
        renders: list[dashboard.DashboardSnapshot] = []

        def stop_after_one_interval(seconds: float) -> None:
            clock.sleep(seconds)
            stop_event.set()

        frames = dashboard.run_render_loop(
            duration_s=None,
            continuous=True,
            read_snapshot=dashboard.demo_snapshot,
            render_png=lambda snapshot, _path, _phase: renders.append(snapshot),
            send=lambda _path: SimpleNamespace(ok=True),
            tempdir=Path("/tmp/dashboard-test"),
            clock=clock.monotonic,
            sleep=stop_after_one_interval,
            stop_event=stop_event,
        )

        self.assertEqual(frames, 1)
        self.assertEqual(len(renders), 1)
        self.assertEqual(clock.sleeps, [1.0])

    def test_continuous_live_mode_closes_native_app_after_stop(self) -> None:
        app = FakeApp(confirmed_connection(), {})
        rendered: list[dashboard.DashboardSnapshot] = []
        stop_event = dashboard.threading.Event()
        stop_event.set()

        frames = dashboard.run_live(
            None,
            continuous=True,
            dependencies=dependencies_for(app, rendered),
            stop_event=stop_event,
            instance_lock=lambda: nullcontext(),
        )

        self.assertEqual(frames, 0)
        self.assertTrue(app.closed)

    def test_second_live_instance_fails_before_trcc_or_usb_boot(self) -> None:
        app = FakeApp(confirmed_connection(), {})
        rendered: list[dashboard.DashboardSnapshot] = []
        with tempfile.TemporaryDirectory() as runtime, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": runtime}, clear=False
        ):
            with dashboard.live_instance_lock():
                with self.assertRaisesRegex(dashboard.DashboardError, "already running"):
                    dashboard.run_live(1.0, dependencies=dependencies_for(app, rendered))

        self.assertFalse(app.closed)
        self.assertEqual(app.commands, [])

    def test_live_lock_remains_held_until_native_close_returns(self) -> None:
        class LockCheckingApp(FakeApp):
            def close(app_self) -> None:
                with self.assertRaisesRegex(dashboard.DashboardError, "already running"):
                    with dashboard.live_instance_lock():
                        pass
                super().close()

        app = LockCheckingApp(confirmed_connection(), {})
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as runtime, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": runtime}, clear=False
        ):
            dashboard.run_live(
                0.1,
                dependencies=dependencies_for(app, []),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
            with dashboard.live_instance_lock():
                pass

        self.assertTrue(app.closed)

    def test_close_exception_releases_the_live_lock_after_teardown(self) -> None:
        class FailingCloseApp(FakeApp):
            def close(app_self) -> None:
                super().close()
                raise OSError("synthetic close failure")

        app = FailingCloseApp(confirmed_connection(), {})
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as runtime, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": runtime}, clear=False
        ):
            with self.assertRaisesRegex(OSError, "synthetic close failure"):
                dashboard.run_live(
                    0.1,
                    dependencies=dependencies_for(app, []),
                    clock=clock.monotonic,
                    sleep=clock.sleep,
                )
            with dashboard.live_instance_lock():
                pass

        self.assertTrue(app.closed)

    def test_live_mode_keeps_unavailable_values_unavailable_and_closes_on_success(self) -> None:
        app = FakeApp(confirmed_connection(), {})
        rendered: list[dashboard.DashboardSnapshot] = []
        clock = FakeClock()

        frames = dashboard.run_live(
            1.1,
            dependencies=dependencies_for(app, rendered),
            clock=clock.monotonic,
            sleep=clock.sleep,
            instance_lock=lambda: nullcontext(),
        )

        self.assertEqual(frames, 2)
        self.assertTrue(app.closed)
        self.assertEqual(len(rendered), 2)
        self.assertIsNone(rendered[0].cpu.temp)
        self.assertIsNone(rendered[0].gpu.temp)
        self.assertIsNone(rendered[0].ram.used_mb)
        self.assertTrue(all(not isinstance(command, FakeSendImage) or command.path.name == "frame.png"
                            for command in app.commands))

    def test_profile_rejection_happens_before_any_image_send_and_closes(self) -> None:
        app = FakeApp(confirmed_connection(pm_byte=63), {"cpu:temp": 50.0})
        rendered: list[dashboard.DashboardSnapshot] = []

        with self.assertRaises(dashboard.DashboardError):
            dashboard.run_live(
                1.0,
                dependencies=dependencies_for(app, rendered),
                instance_lock=lambda: nullcontext(),
            )

        self.assertTrue(app.closed)
        self.assertEqual(rendered, [])
        self.assertFalse(any(isinstance(command, FakeSendImage) for command in app.commands))

    def test_send_error_closes_the_app_and_returns_no_success(self) -> None:
        app = FakeApp(confirmed_connection(), {}, send_ok=False)
        rendered: list[dashboard.DashboardSnapshot] = []

        with self.assertRaises(dashboard.DashboardError):
            dashboard.run_live(
                1.0,
                dependencies=dependencies_for(app, rendered),
                instance_lock=lambda: nullcontext(),
            )

        self.assertTrue(app.closed)

    def test_black_send_failure_closes_app_and_releases_live_lock(self) -> None:
        app = FakeApp(confirmed_connection(), {}, send_ok=False)
        black_frames: list[Path] = []
        with tempfile.TemporaryDirectory() as runtime, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": runtime}, clear=False
        ):
            with self.assertRaisesRegex(dashboard.DashboardError, "image send failed"):
                dashboard.run_live(
                    1.0,
                    dependencies=dependencies_for(
                        app,
                        [],
                        read_dpms_state=lambda: dashboard.DpmsState.SLEEP,
                        black_rendered=black_frames,
                    ),
                )
            with dashboard.live_instance_lock():
                pass

        self.assertTrue(app.closed)
        self.assertEqual(len(black_frames), 1)


class AnimationTests(unittest.TestCase):
    def test_animation_phase_is_deterministic_and_global_shift_is_bounded(self) -> None:
        self.assertEqual(dashboard.animation_phase(42.5), dashboard.animation_phase(42.5))
        initial = dashboard.animation_phase(0.0)
        later = dashboard.animation_phase(42.5)
        self.assertEqual(initial.hue_degrees, 30.0)
        self.assertNotEqual((initial.shift_x, initial.shift_y), (later.shift_x, later.shift_y))
        for second in range(0, 1_801, 7):
            phase = dashboard.animation_phase(float(second))
            self.assertLessEqual(abs(phase.shift_x), 6)
            self.assertLessEqual(abs(phase.shift_y), 6)

    def test_foreground_palette_changes_without_changing_measurements(self) -> None:
        self.assertEqual(dashboard.BACKGROUND, "#000000")
        self.assertEqual(dashboard.CARD_BACKGROUND, "#000000")
        first = dashboard.palette_for_phase(dashboard.animation_phase(0.0))
        later = dashboard.palette_for_phase(dashboard.animation_phase(450.0))
        for field in ("accent", "text", "value", "border", "separator", "muted"):
            with self.subTest(field=field):
                self.assertNotEqual(getattr(first, field), "#000000")
                self.assertNotEqual(getattr(first, field), getattr(later, field))

        snapshot = dashboard.snapshot_from_readings({"cpu:temp": 52.5})
        dashboard.palette_for_phase(dashboard.animation_phase(450.0))
        self.assertEqual(snapshot.cpu.temp, 52.5)
        self.assertEqual(dashboard.format_temperature(snapshot.cpu.temp), "52°C")
        self.assertEqual(dashboard.format_temperature(None), "N/A")


class TemperatureHistoryTests(unittest.TestCase):
    def test_timestamp_coordinates_use_elapsed_time_not_sample_index(self) -> None:
        self.assertEqual(dashboard.timestamp_to_x(700.0, now_s=1_000.0, width=240), 0)
        self.assertEqual(dashboard.timestamp_to_x(850.0, now_s=1_000.0, width=240), 120)
        self.assertEqual(dashboard.timestamp_to_x(1_000.0, now_s=1_000.0, width=240), 240)
        self.assertEqual(dashboard.timestamp_to_x(1_100.0, now_s=1_000.0, width=240), 240)

    def test_history_expires_after_five_minutes_and_stays_bounded(self) -> None:
        history = dashboard.TemperatureHistory()
        history.record(0.0, 40.0)
        history.record(300.0, 41.0)
        history.record(300.1, 42.0)
        self.assertEqual([sample.elapsed_s for sample in history.samples], [300.0, 300.1])

        for second in range(301, 2_000):
            history.record(float(second), 50.0)
        self.assertLessEqual(len(history.samples), dashboard.MAX_TEMPERATURE_HISTORY_SAMPLES)
        self.assertEqual(history.samples[0].elapsed_s, 1_699.0)
        self.assertEqual(history.samples[-1].elapsed_s, 1_999.0)

    def test_preview_histories_have_continuous_cpu_and_gpu_segments(self) -> None:
        preview = dashboard.preview_snapshot(450.0)
        for history in (preview.cpu_history, preview.gpu_history):
            with self.subTest(history=history):
                segments = dashboard.temperature_segments(history, now_s=preview.history_now_s)
                self.assertTrue(any(len(segment) > 1 for segment in segments))
                self.assertLessEqual(len(history), dashboard.MAX_TEMPERATURE_HISTORY_SAMPLES)

    def test_missing_nonfinite_and_long_pauses_break_graph_segments(self) -> None:
        history = dashboard.TemperatureHistory()
        for elapsed_s, temperature in (
            (0.0, 45.0),
            (1.0, math.nan),
            (2.0, math.inf),
            (3.0, None),
            (4.0, 47.0),
            (8.0, 49.0),
            (9.0, 50.0),
        ):
            history.record(elapsed_s, temperature)

        segments = dashboard.temperature_segments(history.samples, now_s=9.0)
        self.assertEqual(
            [[sample.elapsed_s for sample in segment] for segment in segments],
            [[0.0], [4.0], [8.0, 9.0]],
        )
        self.assertEqual(
            dashboard.temperature_to_y(120.0, top=10, height=101),
            (10, True),
        )
        self.assertEqual(
            dashboard.temperature_to_y(-5.0, top=10, height=101),
            (110, True),
        )
        self.assertEqual(dashboard.format_temperature(120.0), "120°C")


class GraphRenderLoopTests(unittest.TestCase):
    def test_startup_has_no_fake_history_and_cpu_gpu_histories_are_independent(self) -> None:
        first = dashboard.snapshot_from_readings({"cpu:temp": 50.0, "gpu:primary:temp": 60.0})
        second = dashboard.snapshot_from_readings({"cpu:temp": 51.0, "gpu:primary:temp": 61.0})
        third = dashboard.snapshot_from_readings({"cpu:temp": 52.0, "gpu:primary:temp": 62.0})
        self.assertEqual(first.cpu_history, ())
        self.assertEqual(first.gpu_history, ())
        self.assertEqual(dashboard.temperature_segments(first.cpu_history, now_s=0.0), ())

        clock = FakeClock()
        snapshots = iter((first, second, third))
        rendered: list[dashboard.DashboardSnapshot] = []
        dashboard.run_render_loop(
            duration_s=2.2,
            read_snapshot=lambda: next(snapshots),
            render_png=lambda snapshot, _path, _phase: rendered.append(snapshot),
            send=lambda _path: SimpleNamespace(ok=True),
            tempdir=Path("/tmp/dashboard-test"),
            clock=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(
            [[sample.value for sample in frame.cpu_history] for frame in rendered],
            [[50.0], [50.0, 51.0], [50.0, 51.0, 52.0]],
        )
        self.assertEqual(
            [[sample.value for sample in frame.gpu_history] for frame in rendered],
            [[60.0], [60.0, 61.0], [60.0, 61.0, 62.0]],
        )


class QtGraphRenderingTests(unittest.TestCase):
    def test_hostname_title_is_injectable_and_elided_before_live_status(self) -> None:
        try:
            from PySide6.QtGui import QImage  # noqa: F401
        except ImportError:
            self.skipTest("PySide6 unavailable under this interpreter; Qt preview is venv-only")

        hostname = "master-" + ("very-long-hostname-" * 20)
        with patch.object(dashboard, "_draw_elided_text", wraps=dashboard._draw_elided_text) as draw_title:
            dashboard.render_dashboard_image(
                dashboard.demo_snapshot(), dashboard.animation_phase(0.0), hostname=hostname
            )

        self.assertEqual(dashboard.dashboard_title("master"), "master")
        self.assertEqual(draw_title.call_args.args[3], hostname)
        title_rect = draw_title.call_args.args[2]
        self.assertLessEqual(title_rect[0] + title_rect[2], 1_004)
        self.assertTrue(dashboard.elide_text(hostname, pixels=28, width=title_rect[2], bold=True).endswith("…"))

    def test_black_renderer_writes_only_zero_rgb_pixels(self) -> None:
        try:
            from PySide6.QtGui import QImage
        except ImportError:
            self.skipTest("PySide6 unavailable under this interpreter; Qt preview is venv-only")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "black.png"
            dashboard.render_black_png(output)
            image = QImage(str(output))

        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (1600, 720))
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                self.assertEqual((color.red(), color.green(), color.blue()), (0, 0, 0))

    def test_offline_preview_draws_synthetic_graphs_on_black_and_uses_phase_palette(self) -> None:
        try:
            from PySide6.QtGui import QImage
        except ImportError:
            self.skipTest("PySide6 unavailable under this interpreter; Qt preview is venv-only")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.png"
            with (
                patch.object(dashboard, "_default_dependencies", side_effect=AssertionError),
                patch.object(dashboard, "run_live", side_effect=AssertionError),
                patch.object(
                    dashboard, "_draw_temperature_graph", wraps=dashboard._draw_temperature_graph
                ) as draw_graph,
            ):
                dashboard.write_preview(output, phase_s=450.0, hostname="preview-host")
            image = QImage(str(output))

        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (1600, 720))
        self.assertEqual(image.pixelColor(0, 0).name().upper(), "#000000")
        self.assertEqual(draw_graph.call_count, 2)
        self.assertEqual([call.args[2][0] for call in draw_graph.call_args_list], [476, 1228])
        self.assertTrue(all(call.args[3] for call in draw_graph.call_args_list))
        self.assertTrue(all(call.args[-1].accent != "#000000" for call in draw_graph.call_args_list))


class CliTests(unittest.TestCase):
    def test_duration_requires_a_positive_finite_value(self) -> None:
        self.assertEqual(dashboard.parse_args([]).duration, 120.0)
        for invalid in ("0", "-1", "nan", "inf", "-inf", "not-a-number"):
            with self.subTest(invalid=invalid), self.assertRaises(SystemExit):
                dashboard.parse_args(["--duration", invalid])

    def test_continuous_is_mutually_exclusive_with_duration(self) -> None:
        self.assertTrue(dashboard.parse_args(["--continuous"]).continuous)
        with self.assertRaises(SystemExit):
            dashboard.parse_args(["--continuous", "--duration", "1"])

    def test_preview_mode_never_loads_trcc_or_touches_live_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.png"
            with (
                patch.object(dashboard, "_default_dependencies", side_effect=AssertionError),
                patch.object(dashboard, "KdeWaylandDpmsProvider", side_effect=AssertionError),
                patch.object(dashboard.subprocess, "run", side_effect=AssertionError),
                patch.object(dashboard, "write_preview") as write_preview,
            ):
                result = dashboard.main(["--preview", str(output)])

        self.assertEqual(result, 0)
        write_preview.assert_called_once_with(output)

    def test_classic_is_the_default_renderer_and_never_loads_cinematic_code(self) -> None:
        with (
            patch.object(dashboard, "_load_sibling", side_effect=AssertionError("classic must stay standalone")),
            patch.object(dashboard, "run_live", return_value=2) as run_live,
        ):
            result = dashboard.main(["--duration", "1"])

        self.assertEqual(result, 0)
        self.assertEqual(dashboard.parse_args([]).renderer, "classic")
        run_live.assert_called_once()
        self.assertFalse(run_live.call_args.kwargs["continuous"])

    def test_cinematic_continuous_route_is_explicit_and_does_not_fall_back_to_classic(self) -> None:
        with (
            patch.object(dashboard, "run_live", side_effect=AssertionError("must not use classic")),
            patch.object(dashboard, "run_cinematic_live", return_value=7) as run_cinematic_live,
        ):
            result = dashboard.main(["--renderer", "cinematic", "--continuous"])

        self.assertEqual(result, 0)
        run_cinematic_live.assert_called_once()
        self.assertTrue(run_cinematic_live.call_args.kwargs["continuous"])
        self.assertIsNone(run_cinematic_live.call_args.args[0])

    def test_cinematic_preview_loads_the_installed_sibling_and_stays_offline(self) -> None:
        try:
            from PySide6.QtGui import QImage  # noqa: F401
        except ImportError:
            self.skipTest("PySide6 unavailable under this interpreter; Qt preview is venv-only")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            installed_dashboard = scripts / "thermalright-dashboard.py"
            installed_cinematic = scripts / "thermalright_cinematic.py"
            shutil.copy2(MODULE_PATH, installed_dashboard)
            shutil.copy2(MODULE_PATH.with_name("thermalright_cinematic.py"), installed_cinematic)
            output = root / "cinematic-demo.png"
            module_name = "installed_thermalright_dashboard"
            spec = importlib.util.spec_from_file_location(module_name, installed_dashboard)
            assert spec is not None and spec.loader is not None
            installed = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {module_name: installed, "thermalright_cinematic": None}):
                spec.loader.exec_module(installed)
                with patch.object(installed, "_default_dependencies", side_effect=AssertionError("no TRCC")):
                    result = installed.main(["--renderer", "cinematic", "--preview", str(output)])

            self.assertEqual(result, 0)
            self.assertTrue(output.is_file())

    def test_daemon_mode_rejects_before_loading_or_invoking_the_live_factory(self) -> None:
        factory_called = False

        def factory() -> object:
            nonlocal factory_called
            factory_called = True
            raise AssertionError("TRCC factory must not run in daemon mode")

        dependencies = dashboard.LiveDependencies(
            app_factory=factory,
            ensure_connected=lambda key: FakeEnsureConnected(key=key),
            send_image=lambda key, path: FakeSendImage(key=key, path=path),
            render_png=lambda snapshot, path, phase=None: None,
            render_black_png=lambda path: None,
            read_dpms_state=lambda: dashboard.DpmsState.UNKNOWN,
        )
        with (
            patch.dict(os.environ, {"TRCC_DAEMON": "1"}, clear=False),
            patch.object(dashboard, "_default_dependencies") as load_dependencies,
            self.assertRaisesRegex(dashboard.DashboardError, "TRCC_DAEMON=1"),
        ):
            dashboard.run_live(1.0, dependencies=dependencies)

        self.assertFalse(factory_called)
        load_dependencies.assert_not_called()

    def test_installed_boot_contract_selects_proxy_only_for_daemon_equals_one(self) -> None:
        try:
            import trcc._boot as boot
        except ImportError:
            self.skipTest("TRCC is unavailable under this interpreter; venv verifies this contract")

        source = inspect.getsource(boot.trcc)
        self.assertIn('os.environ.get(_ENV_FLAG) == "1"', source)
        self.assertIn("ensure_daemon()", source)
        self.assertIn("AppProxy()", source)

    def test_qt_preview_writes_the_frame_dimensions(self) -> None:
        try:
            from PySide6.QtGui import QImage
        except ImportError:
            self.skipTest("PySide6 unavailable under this interpreter; Qt preview is venv-only")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.png"
            dashboard.write_preview(output)
            image = QImage(str(output))

        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (1600, 720))


class DeploymentFileTests(unittest.TestCase):
    ROOT = MODULE_PATH.parents[1]

    def test_udev_rule_grants_active_session_access_to_the_usb_device_only(self) -> None:
        rule = (self.ROOT / "udev/70-rgb-naranja-thermalright.rules").read_text()
        self.assertIn('SUBSYSTEM=="usb"', rule)
        self.assertIn('ENV{DEVTYPE}=="usb_device"', rule)
        self.assertIn('ATTR{idVendor}=="87ad"', rule)
        self.assertIn('ATTR{idProduct}=="70db"', rule)
        self.assertIn('TAG+="uaccess"', rule)
        self.assertNotIn("0666", rule)
        self.assertNotIn("hidraw", rule)

    def test_user_service_stays_private_and_recovers_from_usb_absence(self) -> None:
        unit = (self.ROOT / "systemd/thermalright-dashboard.service").read_text()
        self.assertIn("Environment=TRCC_DAEMON=0", unit)
        self.assertIn("Environment=QT_QPA_PLATFORM=offscreen", unit)
        self.assertIn("thermalright-dashboard.py --continuous --renderer cinematic-gpu", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartSec=10", unit)
        self.assertIn("StartLimitIntervalSec=0", unit)
        self.assertIn("TimeoutStopSec=10", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertNotIn("PrivateDevices", unit)

    def test_installer_default_does_not_activate_and_start_stops_then_starts_complete_bundle(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("installer intentionally refuses root")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture"
            (fixture / "scripts").mkdir(parents=True)
            (fixture / "systemd").mkdir()
            modules = (
                "thermalright-dashboard.py",
                "thermalright_cinematic.py",
                "thermalright_cinematic_runtime.py",
                "thermalright_cinematic_gpu.py",
                    "thermalright_jpeg.py",
            )
            for relative in (
                "scripts/install-thermalright-dashboard.sh",
                *(f"scripts/{name}" for name in modules),
                "systemd/thermalright-dashboard.service",
            ):
                source = self.ROOT / relative
                target = fixture / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            installer = fixture / "scripts/install-thermalright-dashboard.sh"
            installer.chmod(0o755)
            home = root / "private-home"
            home.mkdir(mode=0o700)
            venv_python = home / ".local/share/rgb-naranja/trcc-venv/bin/python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("#!/bin/sh\nexit 0\n")
            venv_python.chmod(0o755)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            log = root / "systemctl.log"
            (fake_bin / "systemctl").write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "printf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
                "case \"${2:-}\" in\n"
                "  is-active) printf 'active\\n' ;;\n"
                "  is-enabled) printf 'enabled\\n' ;;\n"
                "  show) case \"$*\" in *ActiveState*) printf 'active\\n' ;; *MainPID*) printf '200\\n' ;; esac ;;\n"
                "esac\n"
            )
            (fake_bin / "systemctl").chmod(0o755)
            (fake_bin / "sleep").write_text(
                "#!/bin/sh\nprintf 'sleep:%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
            )
            (fake_bin / "sleep").chmod(0o755)
            environment = {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / "config"),
                "PATH": f"{fake_bin}:/usr/local/bin:/usr/bin:/bin",
                "SYSTEMCTL_LOG": str(log),
            }

            default = subprocess.run(
                [str(installer)], cwd=fixture, env=environment, text=True, capture_output=True
            )
            self.assertEqual(default.returncode, 0, default.stderr)
            self.assertIn("no service state was changed", default.stdout)
            self.assertEqual(
                log.read_text().splitlines(),
                [
                    "--user is-active thermalright-dashboard.service",
                    "--user is-enabled thermalright-dashboard.service",
                    "--user daemon-reload",
                ],
            )
            for name in modules:
                self.assertTrue((home / ".local/bin" / name).is_file(), name)

            log.write_text("")
            start = subprocess.run(
                [str(installer), "--start"], cwd=fixture, env=environment, text=True, capture_output=True
            )
            self.assertEqual(start.returncode, 0, start.stderr)
            self.assertIn("Installed, enabled, and started", start.stdout)
            self.assertEqual(
                log.read_text().splitlines(),
                [
                    "--user is-active thermalright-dashboard.service",
                    "--user is-enabled thermalright-dashboard.service",
                    "--user stop thermalright-dashboard.service",
                    "--user daemon-reload",
                    "--user enable thermalright-dashboard.service",
                    "--user start thermalright-dashboard.service",
                    "--user show --property=ActiveState --value thermalright-dashboard.service",
                    "--user show --property=MainPID --value thermalright-dashboard.service",
                    "sleep:3",
                    "--user show --property=ActiveState --value thermalright-dashboard.service",
                    "--user show --property=MainPID --value thermalright-dashboard.service",
                ],
            )
            for name in modules:
                self.assertTrue((home / ".local/bin" / name).is_file(), name)

            log.write_text("")
            extra = subprocess.run(
                [str(installer), "--start", "unexpected"],
                cwd=fixture,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(extra.returncode, 2)
            self.assertEqual(log.read_text(), "")


if __name__ == "__main__":
    unittest.main()
