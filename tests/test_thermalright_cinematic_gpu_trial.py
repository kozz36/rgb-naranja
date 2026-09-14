"""Focused contracts for the finite GPU cinematic-trial wrapper."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "thermalright-cinematic-gpu-trial.py"
SPEC = importlib.util.spec_from_file_location("thermalright_cinematic_gpu_trial", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
gpu_trial = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gpu_trial
SPEC.loader.exec_module(gpu_trial)
trial = gpu_trial.trial


class FakeSidecar:
    instances: list["FakeSidecar"] = []

    def __init__(self, cleanup_state: dict[str, bool]) -> None:
        self.cleanup_state = cleanup_state
        self.info = type("Info", (), {"vendor": "NVIDIA", "renderer": "Fake GPU", "version": "4.6"})()
        self.closed = False
        self.closed_after_trial = False
        self.instances.append(self)

    def render(self, _config: object, _time_s: object) -> object:
        return object()

    def close(self) -> None:
        self.closed = True
        self.closed_after_trial = self.cleanup_state["app_closed"]


def cpu_dependencies() -> object:
    return trial.TrialDependencies(
        app_factory=lambda: None,
        ensure_connected=lambda _key: None,
        send_image=lambda _key, _path: None,
        read_dpms_state=lambda: None,
        render_image=lambda _snapshot, _elapsed: None,
        black_image=lambda: None,
        save_jpeg=lambda _image, _path: True,
    )


class GpuTrialWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeSidecar.instances = []
        self.cpu_field = trial.cinematic.field_rgba
        self.addCleanup(setattr, trial.cinematic, "field_rgba", self.cpu_field)

    def test_loader_reuses_the_trial_cpu_module_identity(self) -> None:
        self.assertIs(sys.modules["thermalright_cinematic"], trial.cinematic)
        self.assertIs(gpu_trial._load_sibling("thermalright_cinematic", "thermalright_cinematic.py"), trial.cinematic)

    def test_encoder_session_loads_the_shared_client_without_the_benchmark(self) -> None:
        executable = Path("/home/1000/.private/rust-dashboard-jpeg")
        expected = object()
        factory = Mock(return_value=expected)
        client = SimpleNamespace(EncoderSession=factory)
        with patch.object(gpu_trial, "_load_sibling", return_value=client) as loader:
            self.assertIs(gpu_trial._new_encoder_session(executable), expected)
        loader.assert_called_once_with("thermalright_jpeg", "thermalright_jpeg.py")
        factory.assert_called_once_with(executable, "420", gpu_trial.JPEG_TIMEOUT_S)
        self.assertNotIn("benchmark_rust_dashboard_jpeg", gpu_trial._new_encoder_session.__code__.co_consts)

    def test_help_and_invalid_duration_never_create_a_gpu_context(self) -> None:
        stdout = io.StringIO()
        with patch.object(gpu_trial, "_new_sidecar", side_effect=AssertionError("GPU must stay lazy")) as sidecar, redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as help_exit:
                gpu_trial.main(["--help"])
            with self.assertRaises(SystemExit):
                gpu_trial.main(["--duration", "60.1"])
            with self.assertRaises(SystemExit):
                gpu_trial.main(["--jpeg-encoder", "/not/validated/on/help", "--help"])
        self.assertEqual(help_exit.exception.code, 0)
        self.assertIn("--jpeg-encoder", stdout.getvalue())
        sidecar.assert_not_called()
        self.assertIs(trial.cinematic.field_rgba, self.cpu_field)

    def test_invalid_encoder_path_rejects_before_gpu_or_trial(self) -> None:
        with patch.object(gpu_trial, "_new_sidecar") as sidecar, patch.object(trial, "main") as trial_main:
            with self.assertRaises(SystemExit):
                gpu_trial.main(["--jpeg-encoder", "relative-encoder"])
            with patch.object(gpu_trial, "_validate_encoder_path", side_effect=ValueError("untrusted artifact")):
                with self.assertRaises(SystemExit):
                    gpu_trial.main(["--jpeg-encoder", "/home/1000/untrusted/rust-dashboard-jpeg"])
        sidecar.assert_not_called()
        trial_main.assert_not_called()

    def test_encoder_trust_requires_private_non_writable_absolute_artifact(self) -> None:
        executable = Path("/home/1000/.private/artifact/rust-dashboard-jpeg")
        directories = (Path("/"), Path("/home"), Path("/home/1000"), Path("/home/1000/.private"), executable.parent)
        entries = {
            str(path): SimpleNamespace(st_mode=stat.S_IFDIR | (0o700 if path == Path("/home/1000") else 0o755), st_uid=1000 if str(path).startswith("/home/1000") else 0)
            for path in directories
        }
        entries[str(executable)] = SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=1000)

        with patch.object(gpu_trial.os, "geteuid", return_value=1000), patch.object(
            gpu_trial.os, "lstat", side_effect=lambda path: entries[str(path)]
        ), patch.object(gpu_trial.os, "access", return_value=True):
            self.assertEqual(gpu_trial._validate_encoder_path(executable), executable)
            entries[str(executable)].st_mode = stat.S_IFLNK | 0o777
            with self.assertRaisesRegex(ValueError, "regular"):
                gpu_trial._validate_encoder_path(executable)
            entries[str(executable)].st_mode = stat.S_IFREG | 0o775
            with self.assertRaisesRegex(ValueError, "writable"):
                gpu_trial._validate_encoder_path(executable)
            entries[str(executable)].st_mode = stat.S_IFREG | 0o755
            entries["/home/1000/.private"].st_mode = stat.S_IFDIR | 0o777
            with self.assertRaisesRegex(ValueError, "writable"):
                gpu_trial._validate_encoder_path(executable)

    def test_off_main_thread_refuses_before_gpu_rust_or_trial_dependencies(self) -> None:
        encoder_path = Path("/home/1000/.private/rust-dashboard-jpeg")
        for use_encoder in (False, True):
            with self.subTest(use_encoder=use_encoder):
                events: list[str] = []
                errors: list[BaseException] = []

                def new_sidecar() -> object:
                    events.append("gpu-created")
                    return SimpleNamespace(
                        info=SimpleNamespace(vendor="NVIDIA", renderer="Fake GPU", version="4.6"),
                        render=lambda _config, _time_s: object(),
                        close=lambda: events.append("gpu-close"),
                    )

                def new_session(_path: Path) -> object:
                    events.append("rust-created")
                    return SimpleNamespace(close=lambda: events.append("rust-close"))

                def trcc_dependencies() -> object:
                    events.append("trcc-dependencies")
                    return cpu_dependencies()

                def fake_trial_main(_argv: list[str]) -> int:
                    events.append("trial-main")
                    self.assertIsInstance(trial._default_dependencies(), trial.TrialDependencies)
                    return 0

                def call_main() -> None:
                    try:
                        arguments = ["--jpeg-encoder", str(encoder_path)] if use_encoder else []
                        gpu_trial.main(arguments)
                    except BaseException as error:
                        errors.append(error)

                with patch.object(trial, "_default_dependencies", side_effect=trcc_dependencies), patch.object(
                    trial, "main", side_effect=fake_trial_main
                ), patch.object(gpu_trial, "_new_sidecar", side_effect=new_sidecar), patch.object(
                    gpu_trial, "_new_encoder_session", side_effect=new_session
                ), patch.object(gpu_trial, "_validate_encoder_path", return_value=encoder_path):
                    worker = threading.Thread(target=call_main)
                    worker.start()
                    worker.join(timeout=1.0)

                self.assertFalse(worker.is_alive())
                self.assertEqual(events, [])
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], RuntimeError)
                self.assertIn("main thread", str(errors[0]))

    def test_gpu_is_created_once_and_closed_after_trial_cleanup(self) -> None:
        state = {"app_closed": False}
        stderr = io.StringIO()

        def completed_main(argv: list[str]) -> int:
            self.assertEqual(argv, ["--duration", "0.1"])
            dependencies = trial._default_dependencies()
            self.assertIsNot(dependencies.render_image, trial._render_live_image)
            self.assertIs(trial.cinematic.field_rgba, self.cpu_field)
            state["app_closed"] = True
            return 0

        with patch.object(trial, "_default_dependencies", side_effect=cpu_dependencies) as defaults, patch.object(
            trial, "main", side_effect=completed_main
        ), patch.object(gpu_trial, "_new_sidecar", side_effect=lambda: FakeSidecar(state)), patch.object(
            gpu_trial, "_new_encoder_session", side_effect=AssertionError("Qt baseline must not spawn Rust")
        ) as encoder_factory, redirect_stderr(stderr):
            self.assertEqual(gpu_trial.main(["--duration", "0.1"]), 0)

        encoder_factory.assert_not_called()
        self.assertEqual(defaults.call_count, 1)
        self.assertEqual(len(FakeSidecar.instances), 1)
        self.assertTrue(FakeSidecar.instances[0].closed_after_trial)
        self.assertIs(trial.cinematic.field_rgba, self.cpu_field)
        self.assertEqual(json.loads(stderr.getvalue())["type"], "cinematic_gpu")

    def test_rust_session_is_persistent_writes_exact_jpeg_and_injects_only_the_field_renderer(self) -> None:
        state = {"app_closed": False}
        payload = b"R" * 4_608_000
        jpeg = b"\xff\xd8rust\xff\xd9"
        session = Mock()
        session.request.return_value = jpeg
        encoder_path = Path("/home/1000/.private/rust-dashboard-jpeg")
        render_calls: list[dict[str, object]] = []

        def completed_main(_argv: list[str]) -> int:
            dependencies = trial._default_dependencies()
            with tempfile.TemporaryDirectory() as directory:
                first, second = Path(directory) / "first.jpeg", Path(directory) / "second.jpeg"
                self.assertTrue(dependencies.save_jpeg(object(), first))
                self.assertTrue(dependencies.save_jpeg(object(), second))
                self.assertEqual(first.read_bytes(), jpeg)
                self.assertEqual(second.read_bytes(), jpeg)
            dependencies.render_image(object(), 0.25)
            return 0

        def record_render(*_args: object, **kwargs: object) -> object:
            render_calls.append(kwargs)
            return object()

        with patch.object(gpu_trial, "_validate_encoder_path", return_value=encoder_path), patch.object(
            gpu_trial, "_new_encoder_session", return_value=session
        ) as factory, patch.object(trial, "_default_dependencies", side_effect=cpu_dependencies), patch.object(
            trial, "main", side_effect=completed_main
        ), patch.object(gpu_trial, "_new_sidecar", side_effect=lambda: FakeSidecar(state)), patch.object(
            gpu_trial, "_rgba8888_payload", return_value=payload
        ), patch.object(trial.cinematic, "render_cinematic_image", side_effect=record_render):
            self.assertEqual(gpu_trial.main(["--duration", "0.1", "--jpeg-encoder", str(encoder_path)]), 0)

        factory.assert_called_once_with(encoder_path)
        self.assertEqual([call.args for call in session.request.call_args_list], [(payload,), (payload,)])
        self.assertEqual(len(render_calls), 1)
        self.assertIs(render_calls[0]["field_renderer"].__self__, FakeSidecar.instances[0])
        self.assertIs(trial.cinematic.field_rgba, self.cpu_field)

    def test_rgba8888_callback_rejects_null_or_wrong_size_and_copies_the_exact_frame(self) -> None:
        from PySide6.QtGui import QImage

        image = QImage(1600, 720, QImage.Format.Format_RGBA8888)
        image.fill(0)
        self.assertEqual(len(gpu_trial._rgba8888_payload(image)), 4_608_000)
        for invalid in (QImage(), QImage(1, 1, QImage.Format.Format_RGBA8888), object()):
            with self.subTest(invalid=type(invalid).__name__), self.assertRaises(ValueError):
                gpu_trial._rgba8888_payload(invalid)

    def test_encoder_request_failure_is_not_replaced_by_qt_or_sent(self) -> None:
        session = Mock()
        session.request.side_effect = RuntimeError("protocol timeout")
        callback = gpu_trial._rust_jpeg_saver(session)
        with patch.object(gpu_trial, "_rgba8888_payload", return_value=b"R" * 4_608_000), tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "protocol timeout"):
                callback(object(), Path(directory) / "frame.jpeg")
        session.request.assert_called_once()

    def test_trial_exception_keeps_cpu_field_immutable_and_closes_sidecar(self) -> None:
        state = {"app_closed": False}

        def failing_main(_argv: list[str]) -> int:
            trial._default_dependencies()
            state["app_closed"] = True
            raise RuntimeError("synthetic trial failure")

        with patch.object(trial, "_default_dependencies", side_effect=cpu_dependencies), patch.object(
            trial, "main", side_effect=failing_main
        ), patch.object(gpu_trial, "_new_sidecar", side_effect=lambda: FakeSidecar(state)), patch.object(
            gpu_trial, "_log_gpu_info"
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic trial failure"):
                gpu_trial.main(["--duration", "0.1"])

        self.assertTrue(FakeSidecar.instances[0].closed_after_trial)
        self.assertIs(trial.cinematic.field_rgba, self.cpu_field)

    def test_trcc_failure_still_closes_rust_then_gpu_and_rust_close_failure_is_visible(self) -> None:
        order: list[str] = []
        state = {"app_closed": False}
        session = Mock()
        encoder_path = Path("/home/1000/.private/rust-dashboard-jpeg")
        sidecar = FakeSidecar(state)

        def close_rust() -> None:
            order.append("rust-close")
            raise RuntimeError("rust close failure")

        def failing_main(_argv: list[str]) -> int:
            trial._default_dependencies()
            order.append("trcc-close")
            raise RuntimeError("TRCC close failure")

        def close_sidecar() -> None:
            order.append("gpu-close")

        session.close.side_effect = close_rust
        sidecar.close = close_sidecar  # type: ignore[method-assign]
        stderr = io.StringIO()
        with patch.object(gpu_trial, "_validate_encoder_path", return_value=encoder_path), patch.object(
            gpu_trial, "_new_encoder_session", return_value=session
        ), patch.object(trial, "_default_dependencies", side_effect=cpu_dependencies), patch.object(
            trial, "main", side_effect=failing_main
        ), patch.object(gpu_trial, "_new_sidecar", return_value=sidecar), patch.object(
            gpu_trial, "_log_gpu_info"), redirect_stderr(stderr):
            with self.assertRaisesRegex(RuntimeError, "TRCC close failure"):
                gpu_trial.main(["--jpeg-encoder", str(encoder_path)])

        self.assertEqual(order, ["trcc-close", "rust-close", "gpu-close"])
        self.assertIn("rust close failure", stderr.getvalue())

    def test_gpu_initialization_failure_reaches_caller_before_cpu_dependencies(self) -> None:
        gpu_error = RuntimeError("no hardware GPU")
        cpu_defaults = Mock(side_effect=AssertionError("CPU dependencies must not start"))

        def dependency_main(_argv: list[str]) -> int:
            trial._default_dependencies()
            return 0

        with patch.object(trial, "_default_dependencies", cpu_defaults), patch.object(
            trial, "main", side_effect=dependency_main
        ), patch.object(gpu_trial, "_new_sidecar", side_effect=gpu_error):
            with self.assertRaisesRegex(RuntimeError, "no hardware GPU"):
                gpu_trial.main(["--duration", "0.1"])

        cpu_defaults.assert_not_called()
        self.assertIs(trial.cinematic.field_rgba, self.cpu_field)


if __name__ == "__main__":
    unittest.main()
