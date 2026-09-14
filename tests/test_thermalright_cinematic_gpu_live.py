"""GPU cinematic live routing contracts without a real GL context or TRCC app."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_thermalright_dashboard as dashboard_tests


dashboard = dashboard_tests.dashboard


class _FakeLogger:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def info(self, message: str, *args: object) -> None:
        self.events.append("gpu.info")
        assert "cinematic-gpu renderer=hardware" in message
        assert args == ("NVIDIA", "Fake GPU", "4.6")


class _FakeRuntime:
    def __init__(self, events: list[str], *, fail_loop: BaseException | None = None) -> None:
        self.events, self.fail_loop = events, fail_loop
        self.logger = _FakeLogger(events)

    def CinematicRuntimeSupport(self, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    def CinematicRuntimeDependencies(self, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    def ProductionMetricsLogger(self, *, started_at: float) -> SimpleNamespace:
        return SimpleNamespace(record=lambda _frame: None)

    def configure_production_logging(self) -> _FakeLogger:
        self.events.append("logging.configure")
        return self.logger

    def run_cinematic_loop(self, *, dependencies: SimpleNamespace, tempdir: Path, **_kwargs: object) -> SimpleNamespace:
        self.events.append("loop")
        if self.fail_loop is not None:
            raise self.fail_loop
        dependencies.render_image(SimpleNamespace(), 0.0)
        dependencies.send_image(tempdir / "frame.jpeg")
        return SimpleNamespace(frames=1)


class _FakeCinematic:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.config = object()

    def render_cinematic_image(self, _snapshot: object, time_s: float, *, hostname: str, field_renderer=None) -> object:
        self.events.append("render")
        assert hostname == dashboard.HOSTNAME
        assert callable(field_renderer)
        assert field_renderer(self.config, time_s) == "gpu-field"
        return object()


class _RecordingApp(dashboard_tests.FakeApp):
    def __init__(self, events: list[str], *, close_error: BaseException | None = None) -> None:
        super().__init__(dashboard_tests.confirmed_connection(), {})
        self.events, self.close_error = events, close_error

    def dispatch(self, command: object) -> object:
        self.events.append("ensure" if isinstance(command, dashboard_tests.FakeEnsureConnected) else "send")
        return super().dispatch(command)

    def close(self) -> None:
        self.events.append("app.close")
        super().close()
        if self.close_error is not None:
            raise self.close_error


class CinematicGpuLiveTests(unittest.TestCase):
    def _run(self, *, runtime: _FakeRuntime, app: _RecordingApp, sidecar_close_error: BaseException | None = None,
             boot_error: BaseException | None = None, jpeg_encoder: Path | None = None) -> tuple[int, list[str]]:
        events = runtime.events

        class Sidecar:
            info = SimpleNamespace(vendor="NVIDIA", renderer="Fake GPU", version="4.6")

            def render(self, config: object, time_s: float) -> str:
                events.append("gpu.render")
                assert config is cinematic.config
                assert time_s == 0.0
                return "gpu-field"

            def close(self) -> None:
                events.append("gpu.close")
                if sidecar_close_error is not None:
                    raise sidecar_close_error

        cinematic = _FakeCinematic(events)
        sidecar = Sidecar()

        def load(module_name: str, _filename: str) -> object:
            return {"thermalright_cinematic_runtime": runtime, "thermalright_cinematic": cinematic,
                    "thermalright_cinematic_gpu": SimpleNamespace(GpuFluidSidecar=lambda: self._construct(events, sidecar))}[module_name]

        @contextmanager
        def lock():
            events.append("lock.enter")
            try:
                yield
            finally:
                events.append("lock.exit")

        def boot() -> _RecordingApp:
            events.append("app.boot")
            if boot_error is not None:
                raise boot_error
            return app

        dependencies = replace(dashboard_tests.dependencies_for(app, []), app_factory=boot)
        kwargs: dict[str, object] = {
            "renderer_backend": "gpu",
            "dependencies": dependencies,
            "instance_lock": lock,
        }
        if jpeg_encoder is not None:
            kwargs.update(continuous=True, jpeg_encoder=jpeg_encoder)
        with patch.object(dashboard, "_load_sibling", side_effect=load):
            frames = dashboard.run_cinematic_live(1.0, **kwargs)
        return frames, events

    @staticmethod
    def _construct(events: list[str], sidecar: object) -> object:
        events.append("gpu.construct")
        return sidecar

    def test_gpu_route_constructs_before_trcc_and_closes_after_app(self) -> None:
        events: list[str] = []
        frames, events = self._run(runtime=_FakeRuntime(events), app=_RecordingApp(events))
        self.assertEqual(frames, 1)
        self.assertEqual(events, [
            "lock.enter", "gpu.construct", "app.boot", "ensure", "logging.configure", "gpu.info", "loop",
            "render", "gpu.render", "send", "app.close", "gpu.close", "lock.exit",
        ])

    def test_gpu_failure_does_not_boot_trcc_or_fall_back_to_cpu(self) -> None:
        events: list[str] = []
        app = _RecordingApp(events)
        runtime, cinematic = _FakeRuntime(events), _FakeCinematic(events)

        def load(module_name: str, _filename: str) -> object:
            if module_name == "thermalright_cinematic_gpu":
                events.append("gpu.construct")
                raise RuntimeError("no hardware GPU")
            return {"thermalright_cinematic_runtime": runtime, "thermalright_cinematic": cinematic}[module_name]

        with patch.object(dashboard, "_load_sibling", side_effect=load), self.assertRaisesRegex(
            dashboard.DashboardError, "cinematic-gpu renderer unavailable: no hardware GPU"
        ):
            dashboard.run_cinematic_live(1.0, renderer_backend="gpu", dependencies=dashboard_tests.dependencies_for(app, []),
                                        instance_lock=lambda: dashboard_tests.nullcontext())
        self.assertEqual(events, ["gpu.construct"])
        self.assertFalse(app.closed)
        self.assertEqual(app.commands, [])

    def test_gpu_closes_when_app_boot_or_loop_fails(self) -> None:
        events: list[str] = []
        with self.assertRaisesRegex(RuntimeError, "app boot failed"):
            self._run(runtime=_FakeRuntime(events), app=_RecordingApp(events), boot_error=RuntimeError("app boot failed"))
        self.assertEqual(events, ["lock.enter", "gpu.construct", "app.boot", "gpu.close", "lock.exit"])

        events = []
        with self.assertRaisesRegex(RuntimeError, "loop failed"):
            self._run(runtime=_FakeRuntime(events, fail_loop=RuntimeError("loop failed")), app=_RecordingApp(events))
        self.assertLess(events.index("app.close"), events.index("gpu.close"))

    def test_both_close_failures_preserve_the_exception_chain(self) -> None:
        events: list[str] = []
        with self.assertRaisesRegex(RuntimeError, "gpu close") as captured:
            self._run(runtime=_FakeRuntime(events), app=_RecordingApp(events, close_error=OSError("app close")),
                      sidecar_close_error=RuntimeError("gpu close"))
        self.assertIsInstance(captured.exception.__context__, OSError)
        self.assertLess(events.index("app.close"), events.index("gpu.close"))

    def test_cli_accepts_gpu_without_gpu_import_and_preview_stays_classic(self) -> None:
        self.assertEqual(dashboard.parse_args(["--renderer", "cinematic-gpu"]).renderer, "cinematic-gpu")
        with patch.object(dashboard, "_load_sibling", side_effect=AssertionError("GPU must stay lazy")), patch.object(
            dashboard, "write_preview"
        ) as preview, patch.object(dashboard, "write_cinematic_preview", side_effect=AssertionError("not cinematic preview")):
            self.assertEqual(dashboard.main(["--renderer", "cinematic-gpu", "--preview", "demo.png"]), 0)
        preview.assert_called_once_with(Path("demo.png"))

    def test_cpu_cinematic_route_never_loads_gpu(self) -> None:
        app = _RecordingApp([])
        stop_event = dashboard.threading.Event()
        stop_event.set()
        original_load = dashboard._load_sibling

        def load(module_name: str, filename: str) -> object:
            if module_name == "thermalright_cinematic_gpu":
                raise AssertionError("CPU cinematic must not load GPU")
            return original_load(module_name, filename)

        with patch.object(dashboard, "_load_sibling", side_effect=load):
            self.assertEqual(dashboard.run_cinematic_live(None, continuous=True, dependencies=dashboard_tests.dependencies_for(app, []),
                                                          stop_event=stop_event, instance_lock=lambda: dashboard_tests.nullcontext()), 0)
        self.assertTrue(app.closed)

    def test_encoder_cli_accepts_only_continuous_gpu_without_dependency_factories(self) -> None:
        encoder = "/home/1000/.private/rust-dashboard-jpeg"
        args = dashboard.parse_args([
            "--renderer", "cinematic-gpu", "--continuous", "--jpeg-encoder", encoder,
        ])
        self.assertEqual(args.jpeg_encoder, encoder)
        for argv in (
            ["--jpeg-encoder", encoder],
            ["--renderer", "cinematic", "--continuous", "--jpeg-encoder", encoder],
            ["--renderer", "cinematic-gpu", "--jpeg-encoder", encoder],
            ["--renderer", "cinematic-gpu", "--continuous", "--preview", "demo.png", "--jpeg-encoder", encoder],
        ):
            with self.subTest(argv=argv), patch.object(
                dashboard, "_default_dependencies", side_effect=AssertionError("must stay pre-hardware")
            ) as factories:
                with self.assertRaises(SystemExit):
                    dashboard.parse_args(argv)
                factories.assert_not_called()

    def test_rust_encoder_is_initialized_once_after_guards_and_closed_between_app_and_gpu(self) -> None:
        events: list[str] = []
        session = SimpleNamespace(close=lambda: events.append("rust.close"))
        encoder = Path("/home/1000/.private/rust-dashboard-jpeg")
        with patch.object(dashboard, "_validate_encoder_path", return_value=encoder), patch.object(
            dashboard, "_new_encoder_session", side_effect=lambda path: events.append("rust.init") or session
        ) as new_session, patch.object(
            dashboard, "_rust_jpeg_saver", return_value=lambda _image, _path: True
        ):
            frames, events = self._run(
                runtime=_FakeRuntime(events), app=_RecordingApp(events), jpeg_encoder=encoder,
            )
        self.assertEqual(frames, 1)
        new_session.assert_called_once_with(encoder)
        self.assertLess(events.index("lock.enter"), events.index("gpu.construct"))
        self.assertLess(events.index("gpu.construct"), events.index("rust.init"))
        self.assertLess(events.index("rust.init"), events.index("loop"))
        self.assertLess(events.index("app.close"), events.index("rust.close"))
        self.assertLess(events.index("rust.close"), events.index("gpu.close"))

    def test_invalid_encoder_trust_and_main_thread_guards_precede_gpu_or_rust(self) -> None:
        encoder = Path("/home/1000/.private/rust-dashboard-jpeg")
        app = _RecordingApp([])
        with patch.object(dashboard, "_validate_encoder_path", side_effect=ValueError("untrusted artifact")), patch.object(
            dashboard, "_load_sibling", side_effect=AssertionError("must stay pre-hardware")
        ), self.assertRaisesRegex(dashboard.DashboardError, "untrusted artifact"):
            dashboard.run_cinematic_live(
                None,
                continuous=True,
                renderer_backend="gpu",
                jpeg_encoder=encoder,
                dependencies=dashboard_tests.dependencies_for(app, []),
                instance_lock=lambda: dashboard_tests.nullcontext(),
            )
        self.assertFalse(app.closed)

        errors: list[BaseException] = []

        def run_on_worker() -> None:
            try:
                dashboard.run_cinematic_live(
                    None,
                    continuous=True,
                    renderer_backend="gpu",
                    jpeg_encoder=encoder,
                    dependencies=dashboard_tests.dependencies_for(_RecordingApp([]), []),
                    instance_lock=lambda: dashboard_tests.nullcontext(),
                )
            except BaseException as error:
                errors.append(error)

        with patch.object(dashboard, "_validate_encoder_path", return_value=encoder), patch.object(
            dashboard, "_load_sibling", side_effect=AssertionError("must stay pre-hardware")
        ):
            worker = dashboard.threading.Thread(target=run_on_worker)
            worker.start()
            worker.join(timeout=1.0)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], dashboard.DashboardError)
        self.assertIn("main thread", str(errors[0]))

    def test_rust_save_failure_prevents_send_and_never_uses_qt_fallback(self) -> None:
        events: list[str] = []
        encoder = Path("/home/1000/.private/rust-dashboard-jpeg")

        class FailingRuntime(_FakeRuntime):
            def run_cinematic_loop(self, *, dependencies: SimpleNamespace, tempdir: Path, **_kwargs: object) -> SimpleNamespace:
                self.events.append("loop")
                dependencies.save_jpeg(object(), tempdir / "frame.jpeg")
                dependencies.send_image(tempdir / "frame.jpeg")
                return SimpleNamespace(frames=1)

        with patch.object(dashboard, "_validate_encoder_path", return_value=encoder), patch.object(
            dashboard, "_new_encoder_session", return_value=SimpleNamespace(close=lambda: events.append("rust.close"))
        ), patch.object(
            dashboard, "_rust_jpeg_saver", return_value=lambda _image, _path: (_ for _ in ()).throw(RuntimeError("protocol timeout"))
        ), self.assertRaisesRegex(RuntimeError, "protocol timeout"):
            self._run(runtime=FailingRuntime(events), app=_RecordingApp(events), jpeg_encoder=encoder)
        self.assertNotIn("send", events)
        self.assertIn("app.close", events)
        self.assertIn("rust.close", events)
        self.assertIn("gpu.close", events)

    def test_help_and_qt_gpu_baseline_do_not_require_the_optional_client(self) -> None:
        with patch.object(dashboard, "_load_sibling", side_effect=AssertionError("help must remain pure")):
            with self.assertRaises(SystemExit) as exit_status:
                dashboard.main(["--help"])
        self.assertEqual(exit_status.exception.code, 0)

        events: list[str] = []
        self._run(runtime=_FakeRuntime(events), app=_RecordingApp(events))
        self.assertNotIn("rust.init", events)


if __name__ == "__main__":
    unittest.main()
