"""System-free contracts for the offline cinematic WebM exporter."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

try:
    from PySide6.QtGui import QImage
except ImportError:
    QImage = None

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "preview_thermalright_cinematic.py"
SPEC = importlib.util.spec_from_file_location("preview_thermalright_cinematic", MODULE_PATH)
assert SPEC and SPEC.loader
exporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = exporter
SPEC.loader.exec_module(exporter)


class FakeStdin:
    def __init__(self, fail_write: bool = False) -> None:
        self.fail_write = fail_write
        self.closed = False
        self.writes: list[int] = []

    def write(self, data: bytes) -> int:
        if self.fail_write:
            raise BrokenPipeError("encoder closed stdin")
        self.writes.append(len(data))
        return len(data)

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(
        self, output_fd: int, *, returncode: int = 0, timeout: bool = False, broken_pipe: bool = False,
        unreapable: bool = False,
    ) -> None:
        self.output_fd = output_fd
        self.pid = 4321
        self.returncode = None
        self.result = returncode
        self.timeout = timeout
        self.unreapable = unreapable
        self.stdin = FakeStdin(broken_pipe)
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.unreapable or (self.timeout and self.wait_calls == 1):
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        self.returncode = self.result
        if self.result == 0:
            os.write(self.output_fd, b"fake-webm")
        return self.result

    def terminate(self) -> None:
        self.terminated = True
        if not self.unreapable:
            self.returncode = self.result

    def kill(self) -> None:
        self.killed = True
        if not self.unreapable:
            self.returncode = self.result


class FakePopen:
    def __init__(self, **settings) -> None:
        self.settings = settings
        self.processes: list[FakeProcess] = []
        self.returncode = 0
        self.timeout = False
        self.broken_pipe = False
        self.unreapable = False

    def __call__(self, argv, **kwargs):
        self.settings["argv"] = argv
        self.settings["kwargs"] = kwargs
        process = FakeProcess(
            int(argv[-1].split(":", 1)[1]), returncode=self.returncode, timeout=self.timeout,
            broken_pipe=self.broken_pipe, unreapable=self.unreapable,
        )
        self.processes.append(process)
        return process


def snapshot_factory():
    return SimpleNamespace(demo=True)


def render_frame(snapshot, time_s: float, hostname: str):
    del snapshot, time_s, hostname
    return object()


FRAME = bytes(exporter.FRAME_BYTES)


def frame_bytes(image) -> bytes:
    del image
    return FRAME


class ExportValidationTests(unittest.TestCase):
    def test_invalid_duration_and_nonintegral_frame_count_reject_before_dependencies(self) -> None:
        invoked = False

        def forbidden(*args, **kwargs):
            nonlocal invoked
            invoked = True
            raise AssertionError("dependencies must not be called")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.webm"
            for duration, fps in ((0, 1), (12.1, 1), (float("inf"), 1), (0.5, 1), (1.00000000001, 1), (1, 2), (1, 25), (1, 30)):
                with self.subTest(duration=duration, fps=fps), self.assertRaises((ValueError, exporter.ExportError)):
                    exporter.export_webm(
                        output, fps=fps, duration_s=duration, hostname="TEST",
                        snapshot_factory=forbidden, render_callback=forbidden,
                        frame_adapter=forbidden, popen_factory=forbidden,
                    )
        self.assertFalse(invoked)

    def test_parser_accepts_24_fps_and_rejects_other_new_rates(self) -> None:
        args = exporter.parse_args(["--output", "preview.webm", "--fps", "24", "--duration", "12"])
        self.assertEqual(args.fps, 24)
        for fps in (25, 30):
            with self.subTest(fps=fps), self.assertRaises(SystemExit):
                exporter.parse_args(["--output", "preview.webm", "--fps", str(fps), "--duration", "12"])

    def test_output_suffix_and_missing_parent_reject(self) -> None:
        with self.assertRaises(ValueError):
            exporter.validate_export_request(Path("preview.mp4"), fps=1, duration_s=1)
        with self.assertRaises(FileNotFoundError):
            exporter.validate_export_request(Path("missing-parent") / "preview.webm", fps=1, duration_s=1)


class ExportLifecycleTests(unittest.TestCase):
    def run_export(self, output: Path, fake: FakePopen):
        ticks = iter(float(value) for value in range(100))
        return exporter.export_webm(
            output, fps=1, duration_s=1, hostname="TEST",
            snapshot_factory=snapshot_factory, render_callback=render_frame,
            frame_adapter=frame_bytes, popen_factory=fake,
            wall_clock=lambda: next(ticks), cpu_clock=lambda: next(ticks),
        )

    def test_streams_fixed_rgba_to_safe_vp9_command_and_reports_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.webm"
            fake = FakePopen()
            metrics = self.run_export(output, fake)

            self.assertTrue(output.exists())
            self.assertEqual(fake.processes[0].stdin.writes, [exporter.FRAME_BYTES])
            self.assertTrue(fake.processes[0].stdin.closed)
            self.assertIn("-n", fake.settings["argv"])
            self.assertIn("libvpx-vp9", fake.settings["argv"])
            self.assertIn("yuv420p", fake.settings["argv"])
            self.assertEqual(len(fake.settings["kwargs"]["pass_fds"]), 1)
            self.assertEqual(metrics["actual_frames"], 1)
            self.assertEqual(metrics["actual_duration_seconds"], 1.0)
            self.assertEqual(metrics["actual_fps"], 1.0)
            self.assertEqual(metrics["encoded_file_bytes"], len(b"fake-webm"))
            self.assertIn("parent_process_cpu_seconds_excluding_ffmpeg", metrics)
            self.assertIn("USB transport was not measured", metrics["measurement_notes"][0])

    def test_uses_natural_fixed_timestamps_without_speeding_animation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.webm"
            timestamps: list[float] = []
            frame = bytes(exporter.FRAME_BYTES)

            def render(snapshot, time_s, hostname):
                del snapshot, hostname
                timestamps.append(time_s)
                return object()

            exporter.export_webm(
                output, fps=6, duration_s=1, hostname="TEST", snapshot_factory=snapshot_factory,
                render_callback=render, frame_adapter=lambda image: frame, popen_factory=FakePopen(),
            )
            self.assertEqual(timestamps, [index / 6 for index in range(6)])

    def test_24_fps_12_seconds_uses_all_288_natural_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.webm"
            timestamps: list[float] = []

            def render(snapshot, time_s, hostname):
                del snapshot, hostname
                timestamps.append(time_s)
                return object()

            metrics = exporter.export_webm(
                output, fps=24, duration_s=12, hostname="TEST", snapshot_factory=snapshot_factory,
                render_callback=render, frame_adapter=frame_bytes, popen_factory=FakePopen(),
            )
            self.assertEqual(metrics["actual_frames"], 288)
            self.assertEqual(timestamps, [index / 24 for index in range(288)])

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.webm"
            output.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                self.run_export(output, FakePopen())
            self.assertEqual(output.read_bytes(), b"keep")

    def test_broken_pipe_render_error_nonzero_and_timeout_leave_no_partial_output(self) -> None:
        cases = ("broken_pipe", "render_error", "nonzero", "timeout")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "preview.webm"
                fake = FakePopen()
                if case == "broken_pipe":
                    fake.broken_pipe = True
                if case == "nonzero":
                    fake.returncode = 7
                if case == "timeout":
                    fake.timeout = True

                callback = render_frame
                if case == "render_error":
                    def callback(snapshot, time_s, hostname):
                        del snapshot, time_s, hostname
                        raise RuntimeError("render failed")

                with self.assertRaises((exporter.ExportError, RuntimeError, BrokenPipeError)):
                    exporter.export_webm(
                        output, fps=1, duration_s=1, hostname="TEST",
                        snapshot_factory=snapshot_factory, render_callback=callback,
                        frame_adapter=frame_bytes, popen_factory=fake,
                    )
                self.assertFalse(output.exists())
                process = fake.processes[0]
                self.assertTrue(process.stdin.closed)
                self.assertTrue(process.terminated or process.killed or process.returncode is not None)


    def test_unreapable_encoder_reports_cleanup_failure_and_chains_first_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.webm"
            fake = FakePopen()
            fake.unreapable = True

            def broken_render(snapshot, time_s, hostname):
                del snapshot, time_s, hostname
                raise RuntimeError("first render failure")

            with self.assertRaises(exporter.ExportError) as raised:
                exporter.export_webm(
                    output, fps=1, duration_s=1, hostname="TEST", snapshot_factory=snapshot_factory,
                    render_callback=broken_render, frame_adapter=frame_bytes, popen_factory=fake,
                )
            self.assertIn("pid=4321", str(raised.exception))
            self.assertIn("status=running", str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, RuntimeError)
            process = fake.processes[0]
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.terminated)
            self.assertTrue(process.killed)
            self.assertFalse(output.exists())

    def test_nonfinite_metrics_prevent_final_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.webm"
            samples = iter((0.0, 1.0, 2.0, 3.0, 4.0, float("nan")))
            with self.assertRaises(exporter.ExportError):
                exporter.export_webm(
                    output, fps=1, duration_s=1, hostname="TEST", snapshot_factory=snapshot_factory,
                    render_callback=render_frame, frame_adapter=frame_bytes, popen_factory=FakePopen(),
                    wall_clock=lambda: next(samples), cpu_clock=lambda: 0.0,
                )
            self.assertFalse(output.exists())


@unittest.skipUnless(QImage is not None, "PySide6 is required only for QImage adapter coverage")
class QImageAdapterTests(unittest.TestCase):
    def test_adapter_accepts_only_tight_fixed_rgba_qimages(self) -> None:
        image = QImage(exporter.WIDTH, exporter.HEIGHT, QImage.Format.Format_ARGB32)
        self.assertEqual(len(exporter.qimage_to_rgba(image)), exporter.FRAME_BYTES)
        wrong_size = QImage(1, 1, QImage.Format.Format_RGBA8888)
        with self.assertRaises(exporter.ExportError):
            exporter.qimage_to_rgba(wrong_size)
        with self.assertRaises(TypeError):
            exporter.qimage_to_rgba(object())


if __name__ == "__main__":
    unittest.main()
