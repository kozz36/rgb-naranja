"""Offline contracts for the optional cinematic OpenGL fluid sidecar."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import math
import os
from pathlib import Path
import random
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SIDECAR_PATH = ROOT / "scripts" / "thermalright_cinematic_gpu.py"
SCRIPTS = str(ROOT / "scripts")
GPU_TESTS_ENABLED = os.environ.get("TRCC_GPU_TESTS") == "1" and importlib.util.find_spec("PySide6") is not None


if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


def load_sidecar():
    spec = importlib.util.spec_from_file_location("thermalright_cinematic_gpu", SIDECAR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def image_rgba(image, np):
    from PySide6.QtGui import QImage

    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    data = np.frombuffer(converted.constBits(), dtype=np.uint8, count=converted.bytesPerLine() * converted.height())
    return data.reshape(converted.height(), converted.bytesPerLine())[:, : converted.width() * 4].reshape(
        converted.height(), converted.width(), 4
    ).copy()


@dataclass(frozen=True)
class Sample:
    elapsed_s: float
    value: float


def render_snapshot():
    history = tuple(Sample(float(time_s), 48.0 + 8.0 * math.sin(time_s / 31.0)) for time_s in range(301))
    return SimpleNamespace(
        cpu=SimpleNamespace(temp=58.0, usage=37.0, power_w=None),
        gpu=SimpleNamespace(temp=63.0, usage=71.0, power_w=None),
        ram=SimpleNamespace(used_mb=12492.8, total_mb=32768.0, percent=38.1),
        cpu_history=history,
        gpu_history=history,
        history_now_s=300.0,
        demo=False,
    )


class GpuSidecarApiContracts(unittest.TestCase):
    def test_sidecar_exposes_a_lazy_context_managed_renderer(self):
        sidecar = load_sidecar()
        self.assertTrue(callable(sidecar.field_rgba))
        self.assertTrue(callable(sidecar.GpuFluidSidecar))
        self.assertTrue(hasattr(sidecar.GpuFluidSidecar, "render"))
        self.assertTrue(hasattr(sidecar.GpuFluidSidecar, "close"))

    def test_request_validation_reuses_the_cpu_config_and_time_contract(self):
        sidecar = load_sidecar()
        config = sidecar._reference().CinematicConfig()
        reference, time_s = sidecar._validate_request(config, -12.5)
        self.assertIs(reference, sidecar._reference())
        self.assertEqual(time_s, -12.5)
        for invalid in (True, False, math.nan, math.inf, -math.inf, "10"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                sidecar._validate_request(config, invalid)
        with self.assertRaises(TypeError):
            sidecar._validate_request(object(), 0.0)

    def test_close_retains_gpu_owners_until_make_current_succeeds(self):
        sidecar = load_sidecar()
        renderer = sidecar.GpuFluidSidecar.__new__(sidecar.GpuFluidSidecar)
        context, surface = Mock(), Mock()
        context.makeCurrent.side_effect = (False, True)
        field_program, color_program, vao, height_fbo, color_fbo = (Mock() for _ in range(5))
        renderer._thread_id, renderer._closed = threading.get_ident(), False
        renderer._context, renderer._surface = context, surface
        renderer._field_program, renderer._color_program, renderer._vao = field_program, color_program, vao
        renderer._height_fbo, renderer._color_fbo, renderer._functions = height_fbo, color_fbo, Mock()

        with self.assertRaisesRegex(RuntimeError, "resources and surface are retained for retry"):
            renderer.close()

        self.assertFalse(renderer._closed)
        self.assertIs(renderer._context, context)
        self.assertIs(renderer._surface, surface)
        self.assertIs(renderer._field_program, field_program)
        self.assertIs(renderer._color_program, color_program)
        self.assertIs(renderer._vao, vao)
        self.assertIs(renderer._height_fbo, height_fbo)
        self.assertIs(renderer._color_fbo, color_fbo)
        surface.destroy.assert_not_called()
        field_program.removeAllShaders.assert_not_called()
        color_program.removeAllShaders.assert_not_called()
        vao.destroy.assert_not_called()
        height_fbo.release.assert_not_called()
        color_fbo.release.assert_not_called()

        renderer.close()

        self.assertTrue(renderer._closed)
        self.assertIsNone(renderer._context)
        self.assertIsNone(renderer._surface)
        self.assertIsNone(renderer._field_program)
        self.assertIsNone(renderer._color_program)
        self.assertIsNone(renderer._vao)
        self.assertIsNone(renderer._height_fbo)
        self.assertIsNone(renderer._color_fbo)
        self.assertIsNone(renderer._functions)
        self.assertEqual(context.makeCurrent.call_count, 2)
        context.doneCurrent.assert_called_once_with()
        surface.destroy.assert_called_once_with()
        field_program.removeAllShaders.assert_called_once_with()
        color_program.removeAllShaders.assert_called_once_with()
        vao.destroy.assert_called_once_with()
        height_fbo.release.assert_called_once_with()
        color_fbo.release.assert_called_once_with()

        renderer.close()
        self.assertEqual(context.makeCurrent.call_count, 2)
        surface.destroy.assert_called_once_with()

    def test_constructor_keeps_its_failure_when_current_cleanup_also_fails(self):
        sidecar = load_sidecar()
        context, surface = Mock(), Mock()
        context.makeCurrent.return_value = False

        def fail_initialization(renderer):
            renderer._context, renderer._surface = context, surface
            raise ValueError("initialization failed")

        with patch.object(sidecar.GpuFluidSidecar, "_initialize", fail_initialization), self.assertRaisesRegex(
            ValueError, "initialization failed"
        ) as captured:
            sidecar.GpuFluidSidecar()

        self.assertIsInstance(captured.exception.__cause__, RuntimeError)
        surface.destroy.assert_not_called()


@unittest.skipUnless(GPU_TESTS_ENABLED, "set TRCC_GPU_TESTS=1 with PySide6 installed to run explicit hardware GPU contracts")
class GpuSidecarHardwareContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import numpy as np

        cls.np = np
        cls.sidecar = load_sidecar()
        cls.renderer = cls.sidecar.GpuFluidSidecar()

    @classmethod
    def tearDownClass(cls):
        cls.renderer.close()

    def test_hardware_context_shape_validation_and_repeatability(self):
        renderer, np = self.renderer, self.np
        reference = self.sidecar._reference()
        self.assertNotIn("llvmpipe", renderer.info.renderer.lower())
        self.assertNotIn("softpipe", renderer.info.renderer.lower())
        self.assertTrue(renderer.info.vendor)
        output = renderer.render(reference.CinematicConfig(), 0.0)
        self.assertEqual(output.shape, (180, 400, 4))
        self.assertEqual(output.dtype, np.uint8)
        self.assertTrue(np.all(output[:, :, 3] == 255))
        config = reference.CinematicConfig(fluid_period_s=17.0, travel_period_s=31.0, title_period_s=13.0, hue_period_s=527.0)
        times = (-0.5, 0.0, 10.0 - 1e-7, 10.0, 10.0 + 1e-7, 30.0, 60.0, 150.0, 525.0, 900.0)
        expected = {time_s: renderer.render(config, time_s) for time_s in times}
        for time_s in reversed(times):
            self.assertTrue(np.array_equal(expected[time_s], renderer.render(config, time_s)))
        for invalid in (True, math.nan, math.inf):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                renderer.render(config, invalid)
        with self.assertRaises(TypeError):
            renderer.render(object(), 0.0)

    def test_gpu_pixels_track_cpu_across_a_deterministic_128_plus_frame_corpus(self):
        renderer, np = self.renderer, self.np
        reference = self.sidecar._reference()
        configurations = (
            reference.CinematicConfig(),
            reference.CinematicConfig(fluid_period_s=17.0, travel_period_s=31.0, title_period_s=13.0, hue_period_s=527.0),
        )
        boundaries = (-0.5, 0.0, 10.0 - 1e-7, 10.0, 10.0 + 1e-7, 20.0, 30.0, 60.0, 150.0, 525.0, 900.0)
        randomizer = random.Random(701)
        random_times = tuple(randomizer.uniform(-1800.0, 1800.0) for _ in range(61))
        deltas, different_pixels, false_positive, false_negative, total_pixels = [], 0, 0, 0, 0
        for config in configurations:
            for time_s in boundaries + random_times:
                cpu = reference.field_rgba(config, time_s)
                gpu = renderer.render(config, time_s)
                delta = np.abs(cpu[:, :, :3].astype(np.int16) - gpu[:, :, :3].astype(np.int16))
                cpu_active, gpu_active = np.any(cpu[:, :, :3], axis=2), np.any(gpu[:, :, :3], axis=2)
                deltas.append(delta)
                different_pixels += int(np.count_nonzero(np.any(delta, axis=2)))
                false_positive += int(np.count_nonzero(gpu_active & ~cpu_active))
                false_negative += int(np.count_nonzero(cpu_active & ~gpu_active))
                total_pixels += cpu_active.size
        values = np.concatenate([delta.reshape(-1) for delta in deltas])
        max_channel, mean_abs = int(values.max()), float(values.mean())
        silhouette_disagreement = (false_positive + false_negative) / total_pixels
        print(
            "GPU field corpus:"
            f" max_channel={max_channel} mean_abs={mean_abs:.6f}/255"
            f" different_pixels={different_pixels / total_pixels:.6%}"
            f" false_positive={false_positive} false_negative={false_negative}"
            f" silhouette_disagreement={silhouette_disagreement:.6%}"
        )
        self.assertLessEqual(max_channel, 2)
        self.assertLessEqual(mean_abs, 0.02)
        self.assertLessEqual(silhouette_disagreement, 0.0001)

    def test_full_cpu_title_and_ui_render_stays_bound_when_gpu_field_is_injected_offline(self):
        np = self.np
        reference = self.sidecar._reference()
        config, time_s = reference.CinematicConfig(), 150.0
        cpu_image = image_rgba(reference.render_cinematic_image(render_snapshot(), time_s, config=config, hostname="MASTER"), np)
        with patch.object(reference, "field_rgba", self.renderer.render):
            gpu_image = image_rgba(reference.render_cinematic_image(render_snapshot(), time_s, config=config, hostname="MASTER"), np)
        self.assertTrue(np.array_equal(cpu_image[:, :, 3], gpu_image[:, :, 3]))
        delta = np.abs(cpu_image[:, :, :3].astype(np.int16) - gpu_image[:, :, :3].astype(np.int16))
        print(f"GPU full render: max_channel={int(delta.max())} mean_abs={float(delta.mean()):.6f}/255")
        self.assertLessEqual(int(delta.max()), 2)
        self.assertLessEqual(float(delta.mean()), 0.02)

    def test_context_manager_releases_gpu_objects_and_can_be_repeated(self):
        config = self.sidecar._reference().CinematicConfig()
        for time_s in (0.0, 10.0):
            with self.sidecar.GpuFluidSidecar() as renderer:
                renderer.render(config, time_s)
            self.assertTrue(renderer._closed)
            with self.assertRaises(RuntimeError):
                renderer.render(config, time_s)


if __name__ == "__main__":
    unittest.main()
