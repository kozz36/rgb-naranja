"""Production contracts for the bounded offline cinematic renderer."""
from __future__ import annotations

from dataclasses import dataclass
import copy
import importlib.util
import math
import os
import random
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "thermalright_cinematic.py"
SPEC = importlib.util.spec_from_file_location("thermalright_cinematic", MODULE_PATH)
assert SPEC and SPEC.loader
cinematic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cinematic
SPEC.loader.exec_module(cinematic)


@dataclass(frozen=True)
class Sample:
    elapsed_s: object
    value: object


@dataclass(frozen=True)
class Reading:
    temp: object
    usage: object
    power_w: object = None


@dataclass(frozen=True)
class Memory:
    used_mb: object
    total_mb: object
    percent: object


@dataclass(frozen=True)
class Snapshot:
    cpu: Reading
    gpu: Reading
    ram: Memory
    cpu_history: tuple[object, ...]
    gpu_history: tuple[object, ...]
    history_now_s: object
    demo: bool = False


def snapshot(*, cpu_temp=58.0, gpu_temp=63.0, cpu_power=None, gpu_power=None, total_mb=32768.0, demo=False):
    history = tuple(Sample(float(t), 48.0 + 8.0 * math.sin(t / 31.0)) for t in range(301))
    return Snapshot(
        cpu=Reading(cpu_temp, 37.0, cpu_power),
        gpu=Reading(gpu_temp, 71.0, gpu_power),
        ram=Memory(12492.8, total_mb, 38.1),
        cpu_history=history,
        gpu_history=history,
        history_now_s=300.0,
        demo=demo,
    )


def graph_samples(*values, start=294.0):
    return tuple(Sample(start + offset, value) for offset, value in enumerate(values))


def adaptive_graph_corpus():
    """Independent CPU/GPU histories spanning the adaptive graph's visual contract."""
    base = snapshot(cpu_power=75.0, gpu_power=31.0, demo=True)

    def case(name, cpu_history, gpu_history, *, time_s=2.5, config=None):
        return name, Snapshot(base.cpu, base.gpu, base.ram, cpu_history, gpu_history, 300.0, demo=True), time_s, (
            cinematic.CinematicConfig() if config is None else config
        )

    return (
        case("flat", graph_samples(55.0, 55.0, 55.0), graph_samples(72.0, 72.0, 72.0)),
        case("fluctuating", graph_samples(50.0, 57.0, 52.0, 59.0, 54.0), graph_samples(69.0, 78.0, 71.0, 76.0)),
        case("physical-extremes", graph_samples(0.0, 2.0, 1.0), graph_samples(108.0, 110.0, 109.0)),
        case("na-gaps", graph_samples(50.0, None, 53.0, 54.0), graph_samples(75.0, None, 77.0, 76.0)),
        case("isolated", (Sample(290.0, 48.0), Sample(296.0, 56.0), Sample(300.0, 51.0)),
             (Sample(290.0, 70.0), Sample(296.0, 78.0), Sample(300.0, 73.0))),
        case("stale-future", (Sample(0.0, 50.0), Sample(299.0, 47.0)),
             (Sample(301.0, 80.0), Sample(300.0, 75.0))),
        case("nonmonotonic-duplicates", (Sample(299.0, 51.0), Sample(300.0, 52.0), Sample(300.0, 53.0), Sample(299.0, 54.0)),
             (Sample(299.0, 81.0), Sample(300.0, 82.0), Sample(300.0, 83.0), Sample(299.0, 84.0))),
        case("out-of-range", graph_samples(-1.0, 47.0, 111.0), graph_samples(120.0, 76.0, -5.0)),
        case("cpu-empty", graph_samples(None, float("nan"), -1.0), graph_samples(72.0, 73.0)),
        case("gpu-empty", graph_samples(52.0, 53.0), graph_samples(None, float("inf"), 111.0)),
        case("rounding-threshold", graph_samples(57.49, 57.50), graph_samples(83.0, 84.0)),
        case("required-padding", graph_samples(50.0, 64.0), graph_samples(70.0, 89.0)),
        case("ui-shift-plus", graph_samples(55.0, 56.0), graph_samples(72.0, 73.0), time_s=2.0),
        case("ui-shift-minus", graph_samples(49.0, 53.0), graph_samples(83.0, 86.0), time_s=6.0),
        case("custom-config", graph_samples(64.0, 66.0), graph_samples(93.0, 95.0), time_s=3.0,
             config=cinematic.CinematicConfig(title_period_s=12.0, hue_period_s=360.0)),
    )


def graph_roi_mask(np, shape):
    allowed = np.zeros(shape[:2], dtype=bool)
    # Graph x origins 80/880 plus the ±6 px UI shift and 1.8 px line pen extension.
    allowed[530:617, 72:667] = True
    allowed[530:617, 872:1467] = True
    return allowed


def draw_fixed_scale_graph(painter, QFont, QPen, view, now, x, shift, accent):
    """Test-only old graph callback; production rendering remains otherwise untouched."""
    segments, _bounds = view
    pen = QPen(accent)
    pen.setWidthF(1.8)
    painter.setPen(pen)
    if now is not None:
        for (left_t, left_v), (right_t, right_v) in segments:
            left_x = x + shift + 578.0 * (left_t - (now - 300.0)) / 300.0
            right_x = x + shift + 578.0 * (right_t - (now - 300.0)) / 300.0
            painter.drawLine(left_x, 544.0 + 32.0 * (1.0 - left_v / 110.0),
                             right_x, 544.0 + 32.0 * (1.0 - right_v / 110.0))
    cinematic._draw_text(painter, QFont, "0—110 °C · 5 min", x + shift, 612.0, 11, accent)


def image_array(image):
    import numpy as np
    converted = image.convertToFormat(image.Format.Format_RGBA8888)
    return np.frombuffer(converted.constBits(), dtype=np.uint8).reshape(converted.height(), converted.width(), 4).copy()


def image_bytes(image):
    return image_array(image).tobytes()


def relative_luminance(rgb):
    def linear(channel):
        normalized = channel / 255.0
        return normalized / 12.92 if normalized <= 0.04045 else ((normalized + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground, background):
    return (max(relative_luminance(foreground), relative_luminance(background)) + 0.05) / (
        min(relative_luminance(foreground), relative_luminance(background)) + 0.05
    )


def foreground_time(hue, period_s=900.0):
    return (hue - 30.0) * period_s / 360.0


def color_rgb(color):
    return color.red(), color.green(), color.blue()


def field_composition_metrics(config, time_s):
    """Measure the actual production composite without assuming a fixed clear region."""
    import numpy as np

    brightness = cinematic.field_rgba(config, time_s)[:, :, :3].max(axis=2)
    mask = brightness > 0
    height, width = mask.shape
    seen, components = set(), []
    for y, x in zip(*mask.nonzero()):
        point = (int(y), int(x))
        if point in seen:
            continue
        queue, pixels = [point], []
        seen.add(point)
        while queue:
            cy, cx = queue.pop()
            pixels.append((cy, cx))
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and (ny, nx) not in seen:
                    seen.add((ny, nx))
                    queue.append((ny, nx))
        if len(pixels) >= 100:
            components.append(pixels)
    interior = mask.copy()
    interior[1:, :] &= mask[:-1, :]
    interior[:-1, :] &= mask[1:, :]
    interior[:, 1:] &= mask[:, :-1]
    interior[:, :-1] &= mask[:, 1:]
    return {
        "mask": mask,
        "brightness": brightness,
        "coverage": float(mask.mean()),
        "components": len(components),
        "boundary_brightness": int(brightness[mask & ~interior].max()),
        "rgb_max": int(brightness.max()),
    }


def canonical_body_metrics(phase):
    """Exercise the same local body function used by the production placements."""
    import numpy as np

    yy, xx = np.mgrid[0:180, 0:400]
    body = cinematic._canonical_body_field(np, xx - 200.0, yy - 90.0, phase, 132.0, 37.3)
    mask = body > 0
    occupied_columns = np.flatnonzero(mask.any(axis=0))
    inset = max(1, round(len(occupied_columns) * 0.15))
    widths = mask[:, occupied_columns[inset:-inset]].sum(axis=0)
    return float(widths.min()), float(np.median(widths))


def production_body_fields(config, travel_time_s, *, intrinsic_time_s=None):
    """Rebuild the renderer's real interaction-aware four-body pipeline."""
    import numpy as np

    yy, xx = np.mgrid[0:180, 0:400]
    return cinematic._body_fields(np, xx, yy, config, travel_time_s, intrinsic_time_s=intrinsic_time_s)


def independent_geometry_field(config, time_s):
    """Baseline-only MAX field used to bound visual growth, not a renderer substitute."""
    import numpy as np

    yy, xx = np.mgrid[0:180, 0:400]
    phase = cinematic._phase(time_s, config.fluid_period_s)
    fields = []
    for center_x, center_y, angle, half_length, half_width, phase_offset in cinematic._body_placements(config, time_s):
        cosine, sine = math.cos(angle), math.sin(angle)
        longitudinal = (xx - center_x) * cosine + (yy - center_y) * sine
        transverse = -(xx - center_x) * sine + (yy - center_y) * cosine
        fields.append(cinematic._canonical_body_field(np, longitudinal, transverse, phase + phase_offset, half_length, half_width))
    return np.maximum.reduce(fields)


def production_geometry_field(config, time_s):
    """Rebuild the actual MAX composite so output tests can isolate surface response."""
    import numpy as np

    return np.maximum.reduce(production_body_fields(config, time_s))


class CinematicPureContracts(unittest.TestCase):
    def test_config_is_fixed_and_rejects_invalid_periods(self):
        config = cinematic.CinematicConfig()
        self.assertEqual((config.width, config.height, config.field_scale), (1600, 720, 4))
        self.assertEqual(config.travel_period_s, 60.0)
        for kwargs in ({"width": 1601}, {"width": 1600.0}, {"height": 719}, {"field_scale": 2},
                       {"fluid_period_s": 0}, {"travel_period_s": -1}, {"travel_period_s": float("nan")},
                       {"title_period_s": float("nan")}, {"hue_period_s": float("inf")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                cinematic.CinematicConfig(**kwargs)

    def test_temperature_digits_are_dimmer_more_chromatic_and_contrast_lava(self):
        config = cinematic.CinematicConfig()
        samples = []
        temperature_rgb = getattr(cinematic, "_temperature_rgb", lambda time_s, current: cinematic._palette(time_s, current)[1])
        for hue in range(360):
            with self.subTest(hue=hue):
                time_s = foreground_time(hue, config.hue_period_s)
                old_rgb = cinematic._hsv_rgb(hue, 0.27, 0.83)
                new_rgb = temperature_rgb(time_s, config)
                lava_rgb = tuple(channel * 64 // 255 for channel in cinematic._hsv_rgb((hue + 180.0) % 360.0, 0.80, 1.0))
                old_luminance = relative_luminance(old_rgb)
                new_luminance = relative_luminance(new_rgb)
                samples.append((hue, old_luminance, new_luminance, contrast_ratio(new_rgb, (0, 0, 0)), contrast_ratio(new_rgb, lava_rgb)))
                self.assertLess(max(new_rgb), 212)
                self.assertGreater(max(new_rgb) - min(new_rgb), max(old_rgb) - min(old_rgb))
                self.assertLess(new_luminance, old_luminance)
                self.assertGreaterEqual(samples[-1][3], 3.0)
                self.assertGreaterEqual(samples[-1][4], 3.0)
                self.assertEqual(new_rgb, temperature_rgb(time_s + config.hue_period_s, config))
        old_average = sum(old for _hue, old, _new, _black_contrast, _lava_contrast in samples) / len(samples)
        new_average = sum(new for _hue, _old, new, _black_contrast, _lava_contrast in samples) / len(samples)
        diagnostics = ", ".join(
            f"{hue}:{old:.3f}/{new:.3f}/{lava_contrast:.2f}"
            for hue, old, new, _black_contrast, lava_contrast in samples if hue % 15 == 0
        )
        major = ", ".join(
            f"{hue}:{cinematic._temperature_rgb(foreground_time(hue), config)}"
            for hue in range(0, 360, 60)
        )
        print(
            f"temperature luminance old/new/lava-contrast (15°): {diagnostics}; "
            f"average={old_average:.3f}/{new_average:.3f}, "
            f"minimum black/lava contrast={min(sample[3] for sample in samples):.2f}/{min(sample[4] for sample in samples):.2f}; "
            f"RGB (60°): {major}"
        )
        self.assertLessEqual(new_average, old_average * 0.70)

    def test_travel_placements_are_signed_wrap_deterministic_and_finite(self):
        config = cinematic.CinematicConfig()
        for time_s in (-30.0, -10.0, 0.0, 10.0, 30.0, 60.0):
            with self.subTest(time_s=time_s):
                self.assertEqual(cinematic._body_placements(config, time_s), cinematic._body_placements(config, time_s))
        self.assertNotEqual(cinematic._body_placements(config, 0.0), cinematic._body_placements(config, 30.0))
        self.assertEqual(cinematic._body_placements(config, 0.0), cinematic._body_placements(config, 60.0))
        for negative, positive in zip(cinematic._body_placements(config, -30.0), cinematic._body_placements(config, 30.0)):
            self.assertAlmostEqual(negative[0], positive[0], places=10)
            self.assertAlmostEqual(negative[1], positive[1], places=10)
        for invalid in (True, float("nan"), float("inf"), -float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                cinematic._body_placements(config, invalid)

    def test_bounded_pair_reaction_is_local_for_perturbed_placements(self):
        config = cinematic.CinematicConfig()
        distant = (
            (70.0, 90.0, 0.0, 100.0, 26.0, 0.0),
            (500.0, 90.0, 0.0, 100.0, 26.0, 2.1),
            (1000.0, 1000.0, 0.0, 50.0, 15.0, 4.2),
            (-1000.0, -1000.0, 0.0, 50.0, 15.0, 0.9),
        )
        encounter = (distant[0], (155.0, 90.0, 0.0, 100.0, 26.0, 2.1), *distant[2:])
        baseline = cinematic._interaction_geometry(config, 0.0, placements=distant)
        reacted = cinematic._interaction_geometry(config, 0.0, placements=encounter)
        self.assertEqual(baseline.placements, distant)
        self.assertGreater(math.dist(reacted.placements[0][:2], encounter[0][:2]), 0.0)
        self.assertGreater(math.dist(reacted.placements[1][:2], encounter[1][:2]), 0.0)
        self.assertLess(math.dist(reacted.placements[0][:2], encounter[0][:2]), 5.0)
        self.assertLess(math.dist(reacted.placements[1][:2], encounter[1][:2]), 5.0)
        self.assertEqual(reacted.placements[2:], encounter[2:])
        self.assertTrue(any(
            contact.first == 0 and contact.second == 1 and contact.strength > 0.1
            for contact in reacted.contacts
        ))

    def test_history_accepts_sample_attributes_and_preserves_gaps_and_order(self):
        samples = (Sample(297.0, 50.0), Sample(298.0, None), Sample(300.0, 55.0))
        original = copy.deepcopy(samples)
        self.assertEqual(cinematic.history_segments((Sample(297.0, 50.0), Sample(300.0, 55.0)), 300.0),
                         [((297.0, 50.0), (300.0, 55.0))])
        self.assertEqual(cinematic.history_segments(((297.0, 50.0), (300.0, 55.0)), 300.0),
                         [((297.0, 50.0), (300.0, 55.0))])
        self.assertEqual(cinematic.history_segments(samples, 300.0), [])
        self.assertEqual(cinematic.history_segments((Sample(300.0, 55.0), Sample(297.0, 50.0)), 300.0), [])
        self.assertEqual(cinematic.history_segments((Sample(-1.0, 50.0), Sample(300.0, 55.0)), 300.0), [])
        self.assertEqual(samples, original)
        self.assertIsNone(cinematic._number(True))
        self.assertIsNone(cinematic._number(False))

    def test_graph_view_uses_only_valid_visible_history_for_bounds_and_preserves_gaps(self):
        history = (
            Sample(-1.0, 50.0), Sample(297.0, 50.0), Sample(298.0, float("inf")),
            Sample(300.0, 55.0), Sample(301.0, 60.0), Sample(300.0, -1.0),
        )
        segments, bounds = cinematic.graph_view(history, 300.0)
        self.assertEqual(segments, [])
        self.assertEqual(bounds, (45.0, 65.0))
        self.assertEqual(cinematic.graph_view((Sample(300.0, 58.0),), 300.0), ([], (50.0, 70.0)))
        self.assertEqual(cinematic.graph_view((Sample(300.0, None),), 300.0), ([], None))
        self.assertEqual(cinematic.history_segments((Sample(296.0, 50.0), Sample(300.0, 55.0)), 300.0), [])

    def test_graph_bounds_are_padded_rounded_minimum_span_and_physically_clamped(self):
        self.assertEqual(cinematic.graph_bounds((55.0,)), (45.0, 65.0))
        self.assertEqual(cinematic.graph_bounds((57.49,)), (45.0, 65.0))
        self.assertEqual(cinematic.graph_bounds((57.50,)), (50.0, 70.0))
        self.assertEqual(cinematic.graph_bounds((55.0, 61.0)), (50.0, 70.0))
        padding_case = cinematic.graph_bounds((50.0, 64.0))
        self.assertIsNotNone(padding_case)
        self.assertLessEqual(padding_case[0], 47.0)
        self.assertGreaterEqual(padding_case[1], 67.0)
        self.assertEqual(padding_case, (45.0, 70.0))
        self.assertEqual(cinematic.graph_bounds((0.0,)), (0.0, 20.0))
        self.assertEqual(cinematic.graph_bounds((110.0,)), (90.0, 110.0))
        self.assertEqual(cinematic.graph_bounds((0.0, 110.0)), (0.0, 110.0))
        self.assertIsNone(cinematic.graph_bounds(()))
        self.assertIsNone(cinematic.graph_bounds((float("nan"), float("inf"))))
        for bounds in ((45.0, 65.0), (45.0, 70.0), (0.0, 20.0), (90.0, 110.0), (0.0, 110.0)):
            with self.subTest(bounds=bounds):
                self.assertTrue(all(math.isfinite(value) and 0.0 <= value <= 110.0 for value in bounds))
                self.assertLess(bounds[0], bounds[1])
                self.assertGreaterEqual(bounds[1] - bounds[0], 20.0)
        self.assertGreaterEqual(abs(cinematic._graph_y(50.0, (45.0, 65.0)) - cinematic._graph_y(55.0, (45.0, 65.0))), 10.0)

    def test_visible_history_streams_invalid_samples_without_a_none_tuple_copy(self):
        class InvalidHistory:
            def __init__(self):
                self.iterated = False

            def __iter__(self):
                self.iterated = True
                yield Sample(300.0, None)
                raise AssertionError("visible history must not be materialized eagerly")

        history = InvalidHistory()
        visible, now = cinematic._visible_history(history, 300.0)
        self.assertEqual(now, 300.0)
        self.assertFalse(history.iterated)
        self.assertFalse(isinstance(visible, tuple))
        self.assertIsNone(next(visible))

    def test_lava_hue_is_the_actual_complementary_background_hue(self):
        self.assertEqual(cinematic.background_hue(0.0), cinematic.lava_hue(0.0))
        self.assertAlmostEqual((cinematic.foreground_hue(0.0) + 180.0) % 360.0, cinematic.background_hue(0.0))
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cinematic.background_hue(value)

    def test_module_remains_offline_only(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("DPMS", source)
        self.assertNotIn("TRCC", source)
        self.assertNotRegex(source, r"(?m)^(?:from|import) .*\b(subprocess|psutil|requests)\b")
        self.assertNotRegex(source, r"(?m)^(?:from|import) .*thermalright-dashboard")

    def test_field_renderer_must_be_callable_before_qt_initialization(self):
        with self.assertRaisesRegex(TypeError, "field_renderer must be callable or None"):
            cinematic.render_cinematic_image(object(), 0.0, field_renderer=object())


@unittest.skipUnless(cinematic.qt_available(), "PySide6 and numpy are required for render contracts")
class CinematicRenderContracts(unittest.TestCase):
    def render(self, data=None, time_s=0.0, config=None, hostname="MASTER"):
        return cinematic.render_cinematic_image(data or snapshot(), time_s, config=config or cinematic.CinematicConfig(), hostname=hostname)

    def test_fixed_shape_and_deterministic_same_runtime_pixels(self):
        import numpy as np

        first = self.render(time_s=2.5)
        second = self.render(time_s=2.5)
        self.assertEqual((first.width(), first.height()), (1600, 720))
        self.assertEqual(image_bytes(first), image_bytes(second))
        for time_s in (-123.456, -0.125):
            with self.subTest(time_s=time_s):
                self.assertTrue(np.array_equal(cinematic.field_rgba(cinematic.CinematicConfig(), time_s),
                                               cinematic.field_rgba(cinematic.CinematicConfig(), time_s)))

    def test_field_renderer_injection_keeps_the_cpu_default_and_uses_the_fixed_rgba_request(self):
        from unittest.mock import patch

        config, time_s, calls = cinematic.CinematicConfig(), 2.5, []
        with patch.object(cinematic, "field_rgba", wraps=cinematic.field_rgba) as cpu_field:
            default = self.render(time_s=time_s, config=config)
        self.assertEqual(cpu_field.call_args.args, (config, time_s))

        def gpu_field(request_config, request_time_s):
            calls.append((request_config, request_time_s))
            rgba = cinematic.field_rgba(request_config, request_time_s)
            self.assertEqual(rgba.shape, (180, 400, 4))
            return rgba

        injected = cinematic.render_cinematic_image(snapshot(), time_s, config=config, field_renderer=gpu_field)
        self.assertEqual(calls, [(config, time_s)])
        self.assertEqual(image_bytes(default), image_bytes(injected))

    def test_static_render_caches_warm_without_changing_pixels_or_rebuilding_expensive_objects(self):
        from unittest.mock import patch

        cinematic._clear_static_render_caches_for_tests()
        self.addCleanup(cinematic._clear_static_render_caches_for_tests)
        with patch.object(cinematic, "_build_coordinate_grid", wraps=cinematic._build_coordinate_grid) as grids, patch.object(
            cinematic, "_create_font", wraps=cinematic._create_font
        ) as fonts:
            cold = self.render(time_s=4.54, hostname="MASTER")
            cold_counts = (grids.call_count, fonts.call_count)
            warm = self.render(time_s=4.54, hostname="MASTER")

        self.assertEqual(image_bytes(cold), image_bytes(warm))
        self.assertEqual(cold_counts[0], 2)
        self.assertGreater(cold_counts[1], 0)
        self.assertEqual((grids.call_count, fonts.call_count), cold_counts)
        self.assertEqual(cinematic._static_render_cache_info()["coordinates"], 2)
        for coordinates in cinematic._COORDINATE_CACHE.values():
            for grid in coordinates:
                self.assertFalse(grid.flags.writeable)
                with self.assertRaises(ValueError):
                    grid[0, 0] = 0

    def test_static_render_caches_are_bounded_and_protect_returned_qt_objects(self):
        cinematic._clear_static_render_caches_for_tests()
        self.addCleanup(cinematic._clear_static_render_caches_for_tests)
        time_s = 4.54
        original_layer, returned_mask, returned_bounds = cinematic.render_title_layer(time_s, hostname="MASTER")
        original_layer_bytes = image_bytes(original_layer)
        original_mask_bytes = image_bytes(returned_mask)
        original_frame = image_bytes(self.render(time_s=time_s, hostname="MASTER"))
        returned_mask.fill(0)
        returned_bounds.translate(100.0, 100.0)
        layout = cinematic.temperature_layout(58.0, 80.0, 760.0)
        layout["number_font"].setPixelSize(1)
        layout["label_font"].setBold(False)

        next_layer, next_mask, next_bounds = cinematic.render_title_layer(time_s, hostname="MASTER")
        next_layout = cinematic.temperature_layout(58.0, 80.0, 760.0)
        self.assertEqual(image_bytes(next_layer), original_layer_bytes)
        self.assertEqual(image_bytes(next_mask), original_mask_bytes)
        self.assertNotEqual(next_bounds, returned_bounds)
        self.assertGreater(next_layout["number_font"].pixelSize(), 1)
        self.assertTrue(next_layout["label_font"].bold())
        self.assertEqual(image_bytes(self.render(time_s=time_s, hostname="MASTER")), original_frame)

        for index in range(cinematic._FONT_CACHE_MAX + 3):
            _np, _Qt, _QColor, QFont, *_rest = cinematic._render_dependencies()
            cinematic._font(QFont, "DejaVu Sans", 400 + index, bool(index % 2))
        static_bytes_before_hosts = cinematic._static_render_cache_info()["static_bytes"]
        for index in range(12):
            cinematic.render_title_layer(time_s, hostname=f"HOST-{index}-WITH-NO-RETAINED-HOSTNAME")
        info = cinematic._static_render_cache_info()
        self.assertEqual(info["static_bytes"], static_bytes_before_hosts)
        self.assertLessEqual(info["fonts"], info["limits"]["fonts"])
        self.assertLess(info["static_bytes"], 3 * 1024 * 1024)

    def test_field_has_a_sixty_second_travel_cycle_and_twenty_second_intrinsic_deformation(self):
        import numpy as np

        config = cinematic.CinematicConfig(hue_period_s=60.0)
        other_title = cinematic.CinematicConfig(title_period_s=13.0, hue_period_s=60.0)
        at_zero = cinematic.field_rgba(config, 0.0)
        at_intrinsic_cycle = cinematic.field_rgba(config, config.fluid_period_s)
        default_at_zero = cinematic.field_rgba(cinematic.CinematicConfig(), 0.0)
        self.assertTrue(np.array_equal(at_zero, cinematic.field_rgba(config, config.travel_period_s)))
        self.assertTrue(np.array_equal(
            (default_at_zero[:, :, :3] > 0).any(axis=2),
            (cinematic.field_rgba(cinematic.CinematicConfig(), 60.0)[:, :, :3] > 0).any(axis=2),
        ))
        self.assertTrue(np.array_equal(cinematic.field_rgba(config, 3.0), cinematic.field_rgba(other_title, 3.0)))
        self.assertGreater(float(((at_zero[:, :, :3] > 0).any(axis=2) != (at_intrinsic_cycle[:, :, :3] > 0).any(axis=2)).mean()), 0.035)
        different_hue = cinematic.CinematicConfig(hue_period_s=75.0)
        self.assertTrue(np.array_equal(
            (cinematic.field_rgba(config, 12.0)[:, :, :3] > 0).any(axis=2),
            (cinematic.field_rgba(different_hue, 12.0)[:, :, :3] > 0).any(axis=2),
        ))
        self.assertFalse(np.array_equal(cinematic.field_rgba(config, 12.0), cinematic.field_rgba(different_hue, 12.0)))

    def test_field_is_continuous_across_fluid_and_travel_signed_phase_wraps(self):
        import numpy as np

        config = cinematic.CinematicConfig()
        step_s = 0.001
        for boundary_s in (10.0, 30.0):
            with self.subTest(boundary_s=boundary_s):
                before = cinematic.field_rgba(config, boundary_s - step_s).astype(int)
                after = cinematic.field_rgba(config, boundary_s + step_s).astype(int)
                mean_difference = float(np.abs(before - after).mean())
                mask_difference = float(((before[:, :, :3] > 0).any(axis=2) != (after[:, :, :3] > 0).any(axis=2)).mean())
                print(f"signed wrap t={boundary_s:g}: rgba={mean_difference:.6f}/255, mask={mask_difference:.4%}")
                self.assertLess(mean_difference, 0.25)
                self.assertLess(mask_difference, 0.01)
                before_centres = cinematic._body_placements(config, boundary_s - step_s)
                after_centres = cinematic._body_placements(config, boundary_s + step_s)
                centre_delta = max(
                    abs(before_body[axis] - after_body[axis])
                    for before_body, after_body in zip(before_centres, after_centres)
                    for axis in (0, 1)
                )
                self.assertLess(centre_delta, 0.1)
                self.assertLessEqual(int(before[:, :, :3].max()), 64)
                self.assertLessEqual(int(after[:, :, :3].max()), 64)

    def test_height_surface_lighting_is_normal_oriented_and_preserves_geometry(self):
        import numpy as np

        config = cinematic.CinematicConfig()
        for time_s in (0.0, 4.0, 8.0):
            with self.subTest(time_s=time_s):
                field = production_geometry_field(config, time_s)
                active = field > 0.0
                rgba = cinematic.field_rgba(config, time_s)
                self.assertTrue((rgba[~active, :3] == 0).all())
                self.assertTrue((rgba[:, :, 3] == 255).all())
                self.assertTrue(np.array_equal(rgba[:, :, 2] > 0, active))
                self.assertGreaterEqual(int(rgba[:, :, 2][active].min()), 1)
                self.assertLessEqual(int(rgba[:, :, :3].max()), 64)

        field = production_geometry_field(config, 0.0)
        height = np.sqrt(field)
        gradient_y, gradient_x = np.gradient(height)
        orientation = 0.55 * gradient_x + 0.35 * gradient_y
        matched_thickness = (field > 0.45) & (field < 0.55)
        low, high = np.quantile(orientation[matched_thickness], (0.20, 0.80))
        shadow = matched_thickness & (orientation <= low)
        lit = matched_thickness & (orientation >= high)
        self.assertGreater(int(shadow.sum()), 100)
        self.assertGreater(int(lit.sum()), 100)
        self.assertLess(abs(float(field[lit].mean()) - float(field[shadow].mean())), 0.02)
        blue = cinematic.field_rgba(config, 0.0)[:, :, 2]
        self.assertGreater(float(blue[lit].mean() - blue[shadow].mean()), 4.0)

    def test_real_field_travels_across_the_screen_with_independent_local_deformation(self):
        import numpy as np

        config = cinematic.CinematicConfig()
        sample_times = (0.0, 6.0, 12.0, 30.0, 45.0, 60.0)
        representative = [(time_s, field_composition_metrics(config, time_s)) for time_s in sample_times]
        print("field diagnostics " + ", ".join(
            f"t={time_s:g}: coverage={metrics['coverage']:.1%}, components={metrics['components']}, "
            f"max/edge={metrics['rgb_max']}/{metrics['boundary_brightness']}"
            for time_s, metrics in representative
        ))
        first = cinematic.field_rgba(config, 0.0)
        self.assertEqual(first.shape, (180, 400, 4))
        self.assertLessEqual(int(first[:, :, :3].max()), 64)
        for time_s, metrics in representative:
            with self.subTest(time_s=time_s):
                body_fields = production_body_fields(config, time_s)
                self.assertEqual(len(body_fields), 4)
                self.assertTrue(all(float(body.min()) >= 0.0 and float(body.max()) <= 1.0 for body in body_fields))
                interaction_field = np.maximum.reduce(body_fields)
                composite = interaction_field > 0.0
                independent = independent_geometry_field(config, time_s)
                independent_coverage = float((independent > 0.0).mean())
                integral_ratio = float(interaction_field.sum() / max(independent.sum(), 1e-6))
                coverage_ratio = metrics["coverage"] / max(independent_coverage, 1e-6)
                print(f"t={time_s:g} interaction proxy coverage={coverage_ratio:.3f} integral={integral_ratio:.3f} (not conservation)")
                self.assertTrue(np.array_equal(metrics["mask"], composite))
                self.assertGreater(metrics["coverage"], 0.05)
                self.assertLess(metrics["coverage"], 0.62)
                self.assertLessEqual(coverage_ratio, 1.12)
                self.assertLessEqual(integral_ratio, 1.12)
                self.assertGreaterEqual(metrics["components"], 1)
                self.assertLessEqual(metrics["components"], 4)
                self.assertLessEqual(metrics["boundary_brightness"], 16)
                for index, body in enumerate(body_fields):
                    with self.subTest(time_s=time_s, body=index):
                        self.assertGreater(int((body > 0.0).sum()), 100)
        active = representative[0][1]["brightness"][representative[0][1]["mask"]]
        self.assertGreaterEqual(len(set(active.tolist())), 12)
        self.assertLessEqual(int(active.max()), 60)

        positions = np.asarray([
            [(placement[0], placement[1]) for placement in cinematic._body_placements(config, time_s)]
            for time_s in sample_times
        ])
        first_twelve_shift = positions[2] - positions[0]
        first_twelve_mask_change = float((representative[0][1]["mask"] != representative[2][1]["mask"]).mean())
        same_intrinsic_mask_change = float((representative[0][1]["mask"] != field_composition_metrics(config, 20.0)["mask"]).mean())
        print(
            "travel t=0→12 centres=" + ", ".join(
                f"{dx:+.1f}/{dy:+.1f}" for dx, dy in first_twelve_shift
            ) + f"; mask={first_twelve_mask_change:.1%}; t=0→20 same-intrinsic mask={same_intrinsic_mask_change:.1%}"
        )
        self.assertGreater(float(np.linalg.norm(first_twelve_shift, axis=1).max()), 100.0)
        self.assertGreater(first_twelve_mask_change, 0.08)
        self.assertGreater(same_intrinsic_mask_change, 0.035)
        self.assertLess(float(first_twelve_shift[:, 0].min()), 0.0)
        self.assertGreater(float(first_twelve_shift[:, 0].max()), 0.0)
        self.assertLess(float(first_twelve_shift[:, 1].min()), 0.0)
        self.assertGreater(float(first_twelve_shift[:, 1].max()), 0.0)

        dense_times = np.linspace(0.0, config.travel_period_s, 121)
        dense_positions = np.asarray([
            [(placement[0], placement[1]) for placement in cinematic._body_placements(config, time_s)]
            for time_s in dense_times
        ])
        ranges = np.ptp(dense_positions, axis=0)
        max_step = float(np.linalg.norm(np.diff(dense_positions, axis=0), axis=2).max())
        print("travel ranges " + ", ".join(f"body={dx:.1f}x/{dy:.1f}y" for dx, dy in ranges) + f"; max 0.5s step={max_step:.1f}")
        self.assertTrue(np.any((ranges[:, 0] > 100.0) & (ranges[:, 1] > 50.0)))
        self.assertLess(float(dense_positions[:, :, 0].min()), 30.0)
        self.assertGreater(float(dense_positions[:, :, 0].max()), 370.0)
        self.assertLess(float(dense_positions[:, :, 1].min()), 0.0)
        self.assertGreater(float(dense_positions[:, :, 1].max()), 180.0)
        self.assertTrue(((dense_positions[:, :, 0] > 160.0) & (dense_positions[:, :, 0] < 240.0) &
                         (dense_positions[:, :, 1] > 60.0) & (dense_positions[:, :, 1] < 120.0)).any())
        self.assertTrue(np.array_equal(dense_positions[0], dense_positions[-1]))
        self.assertLess(max_step, 14.0)

        fixed_travel_early = production_body_fields(config, 0.0, intrinsic_time_s=0.0)
        fixed_travel_late = production_body_fields(config, 0.0, intrinsic_time_s=4.0)
        deformation = max(float(((early > 0.0) != (late > 0.0)).mean()) for early, late in zip(fixed_travel_early, fixed_travel_late))
        print(f"production local deformation at fixed travel t=0→4: {deformation:.1%}")
        self.assertGreater(deformation, 0.025)
        self.assertLess(deformation, 0.24)
        necks = [canonical_body_metrics(phase) for phase in (0.0, 1.2, 2.5, 3.7, 5.0)]
        print("canonical neck diagnostics " + ", ".join(f"{neck:.1f}/{median:.1f}" for neck, median in necks))
        self.assertTrue(all(neck < median * 0.70 for neck, median in necks))
        self.assertGreater(max(neck for neck, _ in necks) - min(neck for neck, _ in necks), 4.0)

    def test_natural_pair_0_1_encounter_connects_at_24fps_with_bounded_response(self):
        import numpy as np

        config = cinematic.CinematicConfig()
        pair_name = "0-1"
        contacts = []
        for frame in range(12 * 24 + 1):
            time_s = frame / 24.0
            contact = next((item for item in cinematic._interaction_geometry(config, time_s).contacts
                            if (item.first, item.second) == (0, 1)), None)
            contacts.append(contact)
        candidates = []
        for frame, contact in enumerate(contacts):
            if contact is None:
                continue
            coupled = production_geometry_field(config, frame / 24.0)
            independent = independent_geometry_field(config, frame / 24.0)
            bridge_values, independent_values = [], []
            for fraction in np.linspace(0.2, 0.8, 7):
                x = round(contact.point_first[0] + fraction * (contact.point_second[0] - contact.point_first[0]))
                y = round(contact.point_first[1] + fraction * (contact.point_second[1] - contact.point_first[1]))
                if 0 <= x < coupled.shape[1] and 0 <= y < coupled.shape[0]:
                    bridge_values.append(float(coupled[y, x]))
                    independent_values.append(float(independent[y, x]))
            if bridge_values:
                candidates.append((max(coupled_value - independent_value
                                       for coupled_value, independent_value in zip(bridge_values, independent_values)),
                                   frame, contact))
        self.assertTrue(candidates)
        neck_gain, peak_frame, peak = max(candidates)
        approach_frames = [frame for frame, item in enumerate(contacts) if item and item.approach > 0.01]
        approach_peak = max((item.approach for item in contacts if item), default=0.0)
        self.assertGreater(peak.strength, 0.20)
        self.assertGreater(peak_frame, 0)
        self.assertTrue(approach_frames)
        self.assertLess(min(approach_frames), peak_frame)
        self.assertLess((contacts[0].strength if contacts[0] else 0.0), peak.strength)
        self.assertTrue(any(item.lagged_proximity > item.current_proximity + 0.01
                            for item in contacts[peak_frame + 1:] if item))
        self.assertGreater(neck_gain, 0.05)

        peak_time_s = peak_frame / 24.0

        unreacted = cinematic._body_placements(config, peak_time_s)
        position_response = max(math.dist(placement[:2], original[:2])
                                for placement, original in zip(cinematic._interaction_geometry(config, peak_time_s).placements, unreacted))
        print(f"pair {pair_name} 24fps peak t={peak_time_s:.3f}s strength={peak.strength:.3f} "
              f"approach={approach_peak:.3f} neck_gain={neck_gain:.3f} position={position_response:.3f}px")
        self.assertGreater(position_response, 0.01)
        self.assertLess(position_response, 5.0)

    def test_title_math_matches_full_grid_reference_exactly(self):
        import numpy as np

        yy, xx = cinematic._static_coordinates(np, cinematic._TITLE_H, cinematic._TITLE_W)
        randomizer = random.Random(44107)
        time_samples = [randomizer.uniform(-3600.0, 3600.0) for _ in range(101)]
        time_samples += [multiple * 8.0 + offset for multiple in range(-4, 5) for offset in (-1e-9, 0.0, 1e-9)]
        for time_s in time_samples:
            with self.subTest(time_s=time_s):
                phase = cinematic._phase(time_s, 8.0)
                surface = 57.0 + 9.0 * np.sin(xx / 59.0 + phase) + 5.0 * np.sin(xx / 21.0 - 2.0 * phase)
                tongues = 13.0 * np.maximum(0.0, np.sin(xx / 37.0 + phase)) ** 3
                pockets = ((xx - 105.0 - 28.0 * math.sin(phase)) / 17.0) ** 2 + ((yy - 42.0) / 10.0) ** 2 < 1.0
                pockets |= ((xx - 355.0 - 20.0 * math.cos(phase)) / 14.0) ** 2 + ((yy - 49.0) / 8.0) ** 2 < 1.0
                expected_liquid = (yy >= surface - tongues) | pockets
                expected_shading = np.clip(
                    0.48 + 0.35 * np.sin(xx / 24.0 + phase) + 0.17 * np.cos(yy / 9.0 - phase), 0.25, 1.0
                )
                liquid, shading = cinematic._title_liquid_and_shading(np, yy, xx, phase)
                self.assertTrue(np.array_equal(liquid, expected_liquid))
                self.assertTrue(np.array_equal(shading, expected_shading))

    def test_title_render_uses_row_column_trigonometry(self):
        import numpy as np
        from unittest.mock import patch

        with patch.object(np, "sin", wraps=np.sin) as sine, patch.object(np, "cos", wraps=np.cos) as cosine:
            cinematic.render_title_layer(4.54, hostname="MASTER")
        self.assertEqual([call.args[0].shape for call in sine.call_args_list], [(1, cinematic._TITLE_W)] * 4)
        self.assertEqual([call.args[0].shape for call in cosine.call_args_list], [(cinematic._TITLE_H, 1)])

    def test_title_layer_is_transparent_clipped_part_filled_and_independent_of_field(self):
        config = cinematic.CinematicConfig(fluid_period_s=20.0, title_period_s=8.0, hue_period_s=900.0)
        layer_a, mask_a, bounds_a = cinematic.render_title_layer(0.0, config=config, hostname="MASTER")
        layer_b, mask_b, bounds_b = cinematic.render_title_layer(2.0, config=config, hostname="MASTER")
        pixels, mask = image_array(layer_a), image_array(mask_a)
        self.assertEqual(image_bytes(mask_a), image_bytes(mask_b))
        self.assertEqual(bounds_a, bounds_b)
        self.assertTrue((pixels[mask[:, :, 3] == 0, 3] == 0).all())
        inside = pixels[mask[:, :, 3] > 240, :3]
        self.assertLess(int(inside.max(axis=1).min()), 30)
        self.assertGreater(int(inside.max(axis=1).max()), 110)
        self.assertNotEqual(image_bytes(layer_a), image_bytes(layer_b))
        fluid_changed, _, _ = cinematic.render_title_layer(2.0, config=cinematic.CinematicConfig(fluid_period_s=13.0), hostname="MASTER")
        self.assertEqual(image_bytes(layer_b), image_bytes(fluid_changed))

    def test_title_outline_hue_changes_without_changing_shape_or_long_header_bounds(self):
        config = cinematic.CinematicConfig(title_period_s=5.0, hue_period_s=90.0)
        before, mask_before, _ = cinematic.render_title_layer(0.0, config=config, hostname="MASTER")
        after, mask_after, bounds = cinematic.render_title_layer(10.0, config=config, hostname="MASTER")
        long_layer, long_mask, long_bounds = cinematic.render_title_layer(0.0, config=config, hostname="HOSTNAME-THAT-IS-TOO-LONG-FOR-THE-HEADER")
        self.assertEqual(image_bytes(mask_before), image_bytes(mask_after))
        self.assertNotEqual(image_bytes(before), image_bytes(after))
        self.assertLessEqual(bounds.right(), 560.0)
        self.assertLessEqual(long_bounds.right(), 560.0)
        self.assertEqual(long_layer.width(), long_mask.width())

    def test_actual_graph_draw_path_uses_adaptive_view_with_independent_scales_and_na_label(self):
        from unittest.mock import patch

        base = snapshot()
        cpu_history = (Sample(297.0, 55.0), Sample(300.0, 61.0))
        gpu_history = (Sample(297.0, 43.0), Sample(300.0, 47.0))
        data = Snapshot(base.cpu, base.gpu, base.ram, cpu_history, gpu_history, 300.0)
        with patch.object(cinematic, "graph_view", wraps=cinematic.graph_view) as graph_view, patch.object(
            cinematic, "_draw_text", wraps=cinematic._draw_text
        ) as draw_text:
            first = self.render(data, time_s=2.5)
            second = self.render(data, time_s=2.5)
        self.assertEqual(image_bytes(first), image_bytes(second))
        self.assertEqual([call.args[:2] for call in graph_view.call_args_list], [(cpu_history, 300.0), (gpu_history, 300.0)] * 2)
        labels = [call.args[2] for call in draw_text.call_args_list]
        self.assertIn("Auto · 50–70 °C · 5 min", labels)
        self.assertIn("Auto · 35–55 °C · 5 min", labels)

        missing = Snapshot(base.cpu, base.gpu, base.ram, (Sample(300.0, None),), (Sample(300.0, None),), 300.0)
        with patch.object(cinematic, "_draw_text", wraps=cinematic._draw_text) as draw_text:
            self.render(missing, time_s=2.5)
        self.assertEqual([call.args[2] for call in draw_text.call_args_list].count("Auto · N/A · 5 min"), 2)

    def test_adaptive_graph_changes_only_graph_rois_for_the_portable_independent_corpus(self):
        import numpy as np
        from unittest.mock import patch

        cases = adaptive_graph_corpus()
        self.assertGreaterEqual(len(cases), 12)
        for name, data, time_s, config in cases:
            with self.subTest(case=name):
                input_repr = repr(data)
                cpu_bounds = cinematic.graph_view(data.cpu_history, data.history_now_s)[1]
                gpu_bounds = cinematic.graph_view(data.gpu_history, data.history_now_s)[1]
                self.assertNotEqual(cpu_bounds, gpu_bounds)
                self.assertEqual(repr(data), input_repr)
                with patch.object(cinematic, "_draw_graph", draw_fixed_scale_graph):
                    for _ in range(5):  # Warm Qt/font state before raw RGBA comparison.
                        before_frame = self.render(data, time_s=time_s, config=config, hostname="DEMO CORPUS")
                for _ in range(5):
                    after_frame = self.render(data, time_s=time_s, config=config, hostname="DEMO CORPUS")
                before, after = image_array(before_frame), image_array(after_frame)
                changed = np.any(before != after, axis=2)
                allowed = graph_roi_mask(np, changed.shape)
                self.assertTrue(np.any(changed & allowed), f"{name} must visibly replace the fixed-scale graph")
                self.assertFalse(np.any(changed & ~allowed), np.argwhere(changed & ~allowed)[:5].tolist())
                self.assertEqual(repr(data), input_repr)

    def test_actual_temperature_draw_path_aligns_power_with_usage_and_graph_edge(self):
        from unittest.mock import patch

        _np, _Qt, _QColor, QFont, QFontMetricsF, *_rest = cinematic._render_dependencies()
        metrics = QFontMetricsF(cinematic._font(QFont, "DejaVu Sans", 22))
        config = cinematic.CinematicConfig()
        for power_w, time_s in ((0.0, -2.0), (23.0, 0.0), (10_000.0, 2.0), (None, 2.0)):
            with self.subTest(power_w=power_w, time_s=time_s), patch.object(
                cinematic, "_draw_text", wraps=cinematic._draw_text
            ) as draw_text:
                self.render(snapshot(cpu_power=power_w, gpu_power=power_w), time_s=time_s)

            calls = [call.args for call in draw_text.call_args_list]
            power_text = cinematic.format_power(power_w)
            power_calls = sorted((call for call in calls if call[2] == power_text), key=lambda call: call[3])
            usage_calls = sorted((call for call in calls if call[2].startswith("Uso ")), key=lambda call: call[3])
            self.assertEqual(len(power_calls), 2)
            self.assertEqual(len(usage_calls), 2)
            self.assertTrue(all("Potencia" not in call[2] for call in calls))
            self.assertNotIn(527.0, [call[4] for call in calls])

            shift = cinematic._ui_shift(time_s, config)
            for power_call, usage_call, graph_right in zip(power_calls, usage_calls, (658.0 + shift, 1458.0 + shift)):
                self.assertEqual(power_call[4], usage_call[4])
                self.assertEqual(power_call[4], 500.0)
                self.assertEqual(power_call[5], usage_call[5])
                self.assertEqual(power_call[5], 22)
                self.assertAlmostEqual(power_call[3] + metrics.boundingRect(power_text).right(), graph_right, places=6)
                self.assertGreater(
                    power_call[3] + metrics.boundingRect(power_text).left(),
                    usage_call[3] + metrics.boundingRect(usage_call[2]).right(),
                )

        self.assertEqual(cinematic.format_power(0.0), "0W")
        self.assertEqual(cinematic.format_power(10_000.0), "10000W")
        self.assertEqual(cinematic.format_power(None), "N/A")
        synthetic = cinematic._synthetic_snapshot()
        self.assertTrue(bool(cinematic._attr(synthetic, "demo", False)))
        self.assertEqual(cinematic._attr(cinematic._attr(synthetic, "cpu"), "power_w"), 75.0)
        self.assertEqual(cinematic._attr(cinematic._attr(synthetic, "gpu"), "power_w"), 31.0)

    def test_temperature_digit_pen_is_production_injected_without_changing_pale_or_layout(self):
        from unittest.mock import patch

        _np, _Qt, QColor, QFont, _QFontMetricsF, QImage, QPainter, _QPainterPath, QPen = cinematic._render_dependencies()
        config = cinematic.CinematicConfig()
        time_s = foreground_time(240.0, config.hue_period_s)
        accent_rgb, pale_rgb = cinematic._palette(time_s, config)
        expected_digit_rgb = cinematic._temperature_rgb(time_s, config)
        with patch.object(cinematic, "_draw_temperature", wraps=cinematic._draw_temperature) as draw_temperature, patch.object(
            cinematic, "_draw_text", wraps=cinematic._draw_text
        ) as draw_text:
            self.render(snapshot(cpu_temp=None, gpu_temp=None), time_s=time_s)

        self.assertEqual(len(draw_temperature.call_args_list), 2)
        self.assertTrue(all(color_rgb(call.kwargs["digit"]) == expected_digit_rgb for call in draw_temperature.call_args_list))
        text_calls = [call.args for call in draw_text.call_args_list]
        power_calls = [call for call in text_calls if call[2] == "N/A"]
        ram_call = next(call for call in text_calls if call[2] == "12,2 / 32 GB")
        self.assertEqual(len(power_calls), 2)
        self.assertTrue(all(color_rgb(call[6]) == pale_rgb for call in power_calls))
        self.assertEqual(color_rgb(ram_call[6]), pale_rgb)

        class RecordingPainter:
            def __init__(self):
                self.pen = None
                self.draws = []

            def setFont(self, _font):
                pass

            def setPen(self, pen):
                self.pen = pen

            def drawText(self, *args):
                self.draws.append((args, self.pen))

        recorder = RecordingPainter()
        with patch.object(cinematic, "_draw_text") as draw_compact_text:
            cinematic._draw_temperature(
                recorder, QColor, QFont, QPen, None, 37.0, "CPU", 80.0, 760.0, 0.0,
                QColor(*accent_rgb), QColor(*pale_rgb), digit=QColor(*expected_digit_rgb), power_w=None,
            )
        self.assertEqual(recorder.draws[1][0][-1], "N/A")
        self.assertEqual(color_rgb(recorder.draws[1][1]), expected_digit_rgb)
        self.assertEqual(color_rgb(draw_compact_text.call_args_list[-1].args[6]), pale_rgb)

        def temperature_alpha_mask(digit_rgb):
            image = QImage(800, 640, QImage.Format.Format_RGBA8888)
            image.fill(0)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            cinematic._draw_temperature(
                painter, QColor, QFont, QPen, 58.0, 37.0, "CPU", 80.0, 760.0, 0.0,
                QColor(*accent_rgb), QColor(*pale_rgb), digit=QColor(*digit_rgb), power_w=None,
            )
            painter.end()
            return image_array(image)[:, :, 3]

        self.assertTrue((temperature_alpha_mask(expected_digit_rgb) == temperature_alpha_mask(pale_rgb)).all())
        layout = cinematic.temperature_layout(58.0, 80.0, 760.0, shift=0.0)
        unchanged = cinematic.temperature_layout(58.0, 80.0, 760.0, shift=0.0)
        self.assertEqual(layout["number"], unchanged["number"])
        self.assertEqual(layout["unit"], unchanged["unit"])

    def test_real_typography_rects_fit_three_digits_na_and_gpu_unit(self):
        image = self.render(snapshot(gpu_temp=100.0), time_s=0.0)
        for temperature in (58.0, 100.0, 110.0, None):
            layout = cinematic.temperature_layout(temperature, 880.0, 1528.0, shift=0.0)
            self.assertFalse(layout["label"].intersects(layout["number"]))
            if temperature is not None:
                self.assertFalse(layout["number"].intersects(layout["unit"]))
                self.assertLessEqual(layout["unit"].right(), 1528.0)
        gpu = cinematic.temperature_layout(100.0, 880.0, 1528.0, shift=0.0)
        rect = gpu["unit"].toAlignedRect()
        self.assertGreater(int(image_array(image.copy(rect))[:, :, :3].max()), 100)
        cpu_number = cinematic.temperature_layout(58.0, 80.0, 760.0, shift=0.0)["number"].toAlignedRect()
        self.assertLessEqual(int(image_array(image.copy(cpu_number))[:, :, :3].max()), 215)

    def test_missing_values_invalid_ram_and_input_snapshot_remain_unchanged(self):
        data = snapshot(cpu_temp=float("nan"), gpu_temp=None, total_mb=0.0)
        original = copy.deepcopy(data)
        invalid = self.render(data, time_s=4.0, hostname="LONG-HOSTNAME-THAT-MUST-ELIDE")
        normal = self.render(snapshot(), time_s=4.0)
        self.assertTrue(math.isnan(data.cpu.temp))
        self.assertEqual(data.gpu, original.gpu)
        self.assertEqual(data.ram, original.ram)
        self.assertEqual(data.cpu_history, original.cpu_history)
        self.assertEqual(data.gpu_history, original.gpu_history)
        self.assertEqual(data.history_now_s, original.history_now_s)
        self.assertNotEqual(image_bytes(invalid.copy(72, 180, 680, 250)), image_bytes(normal.copy(72, 180, 680, 250)))
        self.assertNotEqual(image_bytes(invalid.copy(72, 630, 600, 70)), image_bytes(normal.copy(72, 630, 600, 70)))

    def test_preview_cli_is_headless_synthetic_and_refuses_live_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.png"
            environment = os.environ.copy()
            environment.pop("QT_QPA_PLATFORM", None)
            environment.pop("DISPLAY", None)
            environment.pop("WAYLAND_DISPLAY", None)
            result = subprocess.run(
                [sys.executable, str(MODULE_PATH), "--preview", str(output), "--time", "0"],
                env=environment, text=True, capture_output=True, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())
            with self.assertRaises(FileExistsError):
                cinematic.write_cinematic_preview(output, hostname="TEST")


if __name__ == "__main__":
    unittest.main()
