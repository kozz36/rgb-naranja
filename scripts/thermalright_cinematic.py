"""Deterministic, offline-only cinematic preview renderer.

Frames consume only the supplied snapshot and elapsed time. No sensors, services,
network transport, or system clock are read. Rendering is deterministic within the
installed Qt/font runtime; it intentionally makes no cross-runtime pixel claim.
``write_cinematic_preview`` writes only a synthetic DEMO PNG to a new path.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from collections.abc import Callable
import colorsys
from dataclasses import dataclass
import math
import os
from pathlib import Path
import socket
from types import SimpleNamespace
from typing import Any, Iterable

_WIDTH, _HEIGHT, _FIELD_SCALE = 1600, 720, 4
_FIELD_W, _FIELD_H = _WIDTH // _FIELD_SCALE, _HEIGHT // _FIELD_SCALE
_FIELD_THRESHOLD = 0.70
_GRAPH_Y, _GRAPH_WIDTH, _GRAPH_HEIGHT = 532.0, 578.0, 64.0
_TITLE_W, _TITLE_H = 560, 100
_qt_application = None

# These caches retain only fixed renderer geometry and bounded Qt prototypes. They
# never retain a frame, animated colors, time, snapshot data, or raw host input.
_COORDINATE_CACHE_MAX = 2
_FONT_CACHE_MAX = 32
_COORDINATE_CACHE = OrderedDict()
_FONT_CACHE = OrderedDict()


@dataclass(frozen=True)
class CinematicConfig:
    """Fixed output dimensions and independent fluid, travel, title, and hue periods."""

    width: int = _WIDTH
    height: int = _HEIGHT
    field_scale: int = _FIELD_SCALE
    fluid_period_s: float = 20.0
    travel_period_s: float = 60.0
    title_period_s: float = 8.0
    hue_period_s: float = 900.0

    def __post_init__(self) -> None:
        if (
            any(isinstance(value, bool) or not isinstance(value, int) for value in (self.width, self.height, self.field_scale))
            or (self.width, self.height, self.field_scale) != (_WIDTH, _HEIGHT, _FIELD_SCALE)
        ):
            raise ValueError("cinematic output is fixed at 1600x720 with a 400x180 field")
        for name in ("fluid_period_s", "travel_period_s", "title_period_s", "hue_period_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")


def _finite_time(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("time_s must be finite")
    return float(value)


def _phase(time_s: float, period_s: float) -> float:
    """A continuous, exact-at-period-boundary normalized phase."""
    return math.tau * math.remainder(_finite_time(time_s), period_s) / period_s


def foreground_hue(time_s: float, hue_period_s: float = 900.0) -> float:
    if isinstance(hue_period_s, bool) or not math.isfinite(hue_period_s) or hue_period_s <= 0:
        raise ValueError("hue_period_s must be finite and positive")
    return (30.0 + 360.0 * math.remainder(_finite_time(time_s), hue_period_s) / hue_period_s) % 360.0


def lava_hue(time_s: float, hue_period_s: float = 900.0) -> float:
    """Complementary blue lava hue; color timing never affects fluid geometry."""
    return (foreground_hue(time_s, hue_period_s) + 180.0) % 360.0


def background_hue(time_s: float, hue_period_s: float = 900.0) -> float:
    """The actual lava/background hue, retained as a sensible public helper."""
    return lava_hue(time_s, hue_period_s)


def _render_dependencies():
    # Qt/numpy stay lazy: system Python can import data helpers without either package.
    import numpy as np
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen
    return np, Qt, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen


def _ensure_qt_application() -> None:
    """Retain only the module's Qt app when the caller did not already create one."""
    global _qt_application
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.instance() is None:
        _qt_application = QGuiApplication([])


def qt_available() -> bool:
    try:
        _render_dependencies()
    except ImportError:
        return False
    return True


def _attr(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _value(snapshot: Any, group: str, name: str) -> float | None:
    return _number(_attr(_attr(snapshot, group), name))


def _text_number(value: float | None, decimals: int = 0) -> str:
    return "N/A" if value is None else f"{value:.{decimals}f}".replace(".", ",")


def format_power(value: Any) -> str:
    """Format finite non-negative per-device watts without estimating unavailable input."""
    watts = _number(value)
    return "N/A" if watts is None or watts < 0.0 else f"{watts:.0f}W"


def _sample_values(sample: Any) -> tuple[float | None, float | None]:
    """Read either stable sample attrs or an optional two-item tuple without mutation."""
    if hasattr(sample, "elapsed_s") or hasattr(sample, "value"):
        return _number(_attr(sample, "elapsed_s")), _number(_attr(sample, "value"))
    try:
        elapsed_s, value = sample
    except (TypeError, ValueError):
        return None, None
    return _number(elapsed_s), _number(value)


def _visible_history(history: Iterable[Any], now_s: Any):
    """Stream the existing five-minute validity rules without retaining source history."""
    now = _number(now_s)
    if now is None:
        return (), None
    try:
        iterator = iter(history)
    except TypeError:
        return (), now

    def visible():
        for sample in iterator:  # Preserve supplied order: never sort across a missing sample.
            stamp, reading = _sample_values(sample)
            valid = stamp is not None and reading is not None and now - 300.0 <= stamp <= now and 0.0 <= reading <= 110.0
            yield (stamp, reading) if valid else None

    return visible(), now


def _segments_from_visible(visible) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    previous, segments = None, []
    for point in visible:
        if point is None:
            previous = None
        elif previous is not None and 0.0 < point[0] - previous[0] <= 3.0:
            segments.append((previous, point))
            previous = point
        else:
            previous = point
    return segments


def history_segments(history: Iterable[Any], now_s: Any) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Keep only adjacent valid 0..110°C samples; every malformed/gap sample breaks a line."""
    visible, _now = _visible_history(history, now_s)
    return _segments_from_visible(visible)


def _graph_bounds_for_range(minimum: float, maximum: float) -> tuple[float, float]:
    """Round outward from a physical range, retaining the existing centered minimum-span policy."""
    lower = max(0.0, 5.0 * math.floor((minimum - 3.0) / 5.0))
    upper = min(110.0, 5.0 * math.ceil((maximum + 3.0) / 5.0))
    if upper - lower < 20.0:
        center = 5.0 * math.floor((minimum + maximum) / 10.0 + 0.5)
        lower = min(lower, max(0.0, center - 10.0))
        upper = max(upper, min(110.0, center + 10.0))
    if upper - lower < 20.0:
        lower, upper = (0.0, 20.0) if lower <= 0.0 else (90.0, 110.0)
    return lower, upper


def graph_bounds(values: Iterable[Any]) -> tuple[float, float] | None:
    """Return padded five-degree display bounds for finite physical temperatures."""
    minimum = maximum = None
    for item in values:
        value = _number(item)
        if value is None or not 0.0 <= value <= 110.0:
            continue
        minimum = value if minimum is None else min(minimum, value)
        maximum = value if maximum is None else max(maximum, value)
    return None if minimum is None else _graph_bounds_for_range(minimum, maximum)


def graph_view(history: Iterable[Any], now_s: Any):
    """Build one deterministic graph view from visible history, including isolated readings."""
    visible, _now = _visible_history(history, now_s)
    previous, segments = None, []
    minimum = maximum = None
    for point in visible:
        if point is None:
            previous = None
            continue
        stamp, reading = point
        minimum = reading if minimum is None else min(minimum, reading)
        maximum = reading if maximum is None else max(maximum, reading)
        if previous is not None and 0.0 < stamp - previous[0] <= 3.0:
            segments.append((previous, point))
        previous = point
    return segments, None if minimum is None else _graph_bounds_for_range(minimum, maximum)


def _graph_y(value: float, bounds: tuple[float, float]) -> float:
    lower, upper = bounds
    return _GRAPH_Y + _GRAPH_HEIGHT * (upper - min(upper, max(lower, value))) / (upper - lower)


def _hsv_rgb(hue: float, saturation: float, value: float) -> tuple[int, int, int]:
    red, green, blue = colorsys.hsv_to_rgb((hue % 360.0) / 360.0, saturation, value)
    return round(red * 255), round(green * 255), round(blue * 255)


def _color(QColor, rgb: tuple[int, int, int], alpha: int = 255):
    return QColor(*rgb, alpha)


def _palette(time_s: float, config: CinematicConfig):
    hue = foreground_hue(time_s, config.hue_period_s)
    return _hsv_rgb(hue, 0.70, 0.90), _hsv_rgb(hue, 0.27, 0.83)


def _temperature_rgb(time_s: float, config: CinematicConfig) -> tuple[int, int, int]:
    """Return the dedicated large-temperature digit color for the foreground hue."""
    hue = foreground_hue(time_s, config.hue_period_s)
    # Red and blue/purple need a modest lift over their dim lava complements.
    # 211 stays below the legacy pale peak (212) while saturation rises from 0.27.
    red_lift = max(0.0, math.cos(math.radians(hue))) ** 4
    blue_lift = max(0.0, math.cos(math.radians(hue - 255.0)))
    value = min(211.0 / 255.0, 0.69 + 0.145 * max(red_lift, blue_lift))
    return _hsv_rgb(hue, 0.40, value)


def _canonical_body_field(np, longitudinal, transverse, phase: float, half_length: float, half_width: float):
    """A curved, necking body in local coordinates; production only changes its placement."""
    u = longitudinal / half_length
    edge = np.clip(1.0 - u * u, 0.0, 1.0)
    centerline = half_width * edge * (
        0.456 * np.sin(math.pi * u + phase) + 0.188 * np.sin(2.0 * math.pi * u - 2.0 * phase)
    )
    transport = math.sin(phase - 0.60)
    left_lobe = -0.58 + 0.08 * math.sin(phase + 0.35)
    right_lobe = 0.58 + 0.08 * math.sin(phase - 0.50)
    neck = 0.16 * math.sin(phase + 0.80)
    radius = half_width * (
        1.0
        + (0.483 + 0.268 * transport) * np.exp(-((u - left_lobe) / 0.24) ** 2)
        + (0.483 - 0.268 * transport) * np.exp(-((u - right_lobe) / 0.24) ** 2)
        - (0.456 + 0.134 * math.cos(phase - 0.30)) * np.exp(-((u - neck) / 0.22) ** 2)
    )
    return np.maximum(0.0, 1.0 - u * u - ((transverse - centerline) / radius) ** 2)


def _body_placements(config: CinematicConfig, time_s: float):
    """Return the four independently travelling body placements for the production field."""
    if not isinstance(config, CinematicConfig):
        raise TypeError("config must be a CinematicConfig")
    travel = _phase(_finite_time(time_s), config.travel_period_s)
    # Integral harmonics keep closed configured-period paths continuous through
    # math.remainder's signed wrap while each body follows a distinct orbit.
    return (
        (
            104.0 + 150.0 * math.sin(travel + 0.20) + 24.0 * math.sin(2.0 * travel - 0.60),
            10.0 + 60.0 * math.cos(travel - 0.40) + 12.0 * math.sin(2.0 * travel + 0.70),
            1.16, 115.0, 29.0, 0.0,
        ),
        (
            165.0 + 132.0 * math.sin(travel + 2.25) + 20.0 * math.sin(2.0 * travel + 1.10),
            -12.0 + 62.0 * math.cos(travel + 1.45) + 14.0 * math.cos(2.0 * travel - 0.50),
            1.30, 108.0, 28.0, 2.1,
        ),
        (
            300.0 + 130.0 * math.cos(travel + 0.60) + 22.0 * math.sin(2.0 * travel - 0.90),
            93.0 + 66.0 * math.sin(travel + 0.15) + 14.0 * math.sin(2.0 * travel - 1.30),
            1.12, 120.0, 37.0, 4.2,
        ),
        (
            140.0 + 118.0 * math.sin(travel + 4.10) + 18.0 * math.cos(2.0 * travel + 0.25),
            175.0 + 45.0 * math.cos(travel + 3.60) + 10.0 * math.sin(2.0 * travel + 0.50),
            1.48, 54.0, 24.0, 0.9,
        ),
    )


@dataclass(frozen=True)
class _PairContact:
    """A local, kinematic visual contact; it is not a physical collision result."""

    first: int
    second: int
    strength: float
    current_proximity: float
    lagged_proximity: float
    approach: float
    point_first: tuple[float, float]
    point_second: tuple[float, float]
    neck_radius: float


@dataclass(frozen=True)
class _InteractionGeometry:
    """Responsive placements and local joins for the four production bodies."""

    placements: tuple[tuple[float, float, float, float, float, float], ...]
    contacts: tuple[_PairContact, ...]


_CAPSULE_SAMPLES = tuple(
    (u, 1.0 - u * u, math.sin(math.pi * u), math.cos(math.pi * u),
     math.sin(2.0 * math.pi * u), math.cos(2.0 * math.pi * u),
     math.sin(1.9 * u), math.cos(1.9 * u))
    for u in (-0.76, -0.38, 0.0, 0.38, 0.76)
)


def _placement_capsules(placement, phase: float):
    """Approximate one curved canonical body with five overlapping local capsules."""
    center_x, center_y, angle, half_length, half_width, phase_offset = placement
    cosine, sine = math.cos(angle), math.sin(angle)
    body_phase = phase + phase_offset
    phase_sine, phase_cosine = math.sin(body_phase), math.cos(body_phase)
    double_sine, double_cosine = 2.0 * phase_sine * phase_cosine, phase_cosine * phase_cosine - phase_sine * phase_sine
    capsules = []
    for u, edge, sin_u, cos_u, sin_double_u, cos_double_u, sin_radius_u, cos_radius_u in _CAPSULE_SAMPLES:
        centerline = half_width * edge * (
            0.456 * (sin_u * phase_cosine + cos_u * phase_sine)
            + 0.188 * (sin_double_u * double_cosine - cos_double_u * double_sine)
        )
        radius_sine = phase_sine * cos_radius_u + phase_cosine * sin_radius_u
        radius = half_width * (0.64 + 0.30 * edge) * (1.0 + 0.10 * radius_sine)
        local_x, local_y = u * half_length, centerline
        capsules.append((
            center_x + local_x * cosine - local_y * sine,
            center_y + local_x * sine + local_y * cosine,
            max(5.0, radius),
        ))
    return capsules


def _capsule_bounds(capsules):
    left = min(center_x - radius for center_x, _, radius in capsules)
    right = max(center_x + radius for center_x, _, radius in capsules)
    top = min(center_y - radius for _, center_y, radius in capsules)
    bottom = max(center_y + radius for _, center_y, radius in capsules)
    return left, top, right, bottom


def _bounds_can_interact(first_bounds, second_bounds) -> bool:
    """Cull only pairs that are provably outside the smooth capsule gate's support."""
    first_left, first_top, first_right, first_bottom = first_bounds
    second_left, second_top, second_right, second_bottom = second_bounds
    dx = max(first_left - second_right, second_left - first_right, 0.0)
    dy = max(first_top - second_bottom, second_top - first_bottom, 0.0)
    return dx * dx + dy * dy <= 46.0 * 46.0


def _capsule_pair_contact(first_capsules, second_capsules):
    """Blend all capsule contacts, rather than switching a closest-capsule argmin."""
    weight_sum = point_first_x = point_first_y = point_second_x = point_second_y = radius_sum = 0.0
    hypot = math.hypot
    for first_x, first_y, first_radius in first_capsules:
        for second_x, second_y, second_radius in second_capsules:
            dx, dy = second_x - first_x, second_y - first_y
            distance = hypot(dx, dy)
            fraction = (46.0 - (distance - first_radius - second_radius)) / 56.0
            if fraction <= 0.0:
                continue
            weight = 1.0 if fraction >= 1.0 else fraction * fraction * (3.0 - 2.0 * fraction)
            if distance <= 1e-6:
                direction_x, direction_y = 1.0, 0.0
            else:
                direction_x, direction_y = dx / distance, dy / distance
            first_reach = min(first_radius, distance * 0.5)
            second_reach = min(second_radius, distance * 0.5)
            point_first_x += weight * (first_x + direction_x * first_reach)
            point_first_y += weight * (first_y + direction_y * first_reach)
            point_second_x += weight * (second_x - direction_x * second_reach)
            point_second_y += weight * (second_y - direction_y * second_reach)
            radius_sum += weight * min(first_radius, second_radius)
            weight_sum += weight
    if weight_sum == 0.0:
        return 0.0, (0.0, 0.0), (0.0, 0.0), 0.0
    return (
        1.0 - math.exp(-0.45 * weight_sum),
        (point_first_x / weight_sum, point_first_y / weight_sum),
        (point_second_x / weight_sum, point_second_y / weight_sum),
        radius_sum / weight_sum,
    )


def _capsule_pair_proximity(first_capsules, second_capsules):
    """Fast history-only continuous proximity; points are needed only for the current join."""
    weight_sum = 0.0
    hypot = math.hypot
    for first_x, first_y, first_radius in first_capsules:
        for second_x, second_y, second_radius in second_capsules:
            fraction = (46.0 - (hypot(second_x - first_x, second_y - first_y) - first_radius - second_radius)) / 56.0
            if fraction > 0.0:
                weight_sum += 1.0 if fraction >= 1.0 else fraction * fraction * (3.0 - 2.0 * fraction)
    return 1.0 - math.exp(-0.45 * weight_sum)


def _interaction_placements(config: CinematicConfig, time_s: float, placements):
    if placements is None:
        current = _body_placements(config, time_s)
        history = (_body_placements(config, time_s - 0.2), _body_placements(config, time_s - 0.4))
    else:
        try:
            current = tuple(tuple(placement) for placement in placements)
        except TypeError as error:
            raise TypeError("placements must contain four six-value placements") from error
        if len(current) != 4 or any(len(placement) != 6 for placement in current):
            raise ValueError("placements must contain four six-value placements")
        if any(not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                       for value in placement) for placement in current):
            raise ValueError("placement values must be finite numbers")
        history = (current, current)
    return current, history


def _interaction_geometry(config: CinematicConfig, time_s: float, *, placements=None) -> _InteractionGeometry:
    """Return a bounded kinematic encounter approximation for supplied or production placements.

    All six body pairs are continuously gated from five-capsule proximity.  The
    0.2/0.4s analytic history gives visual release without retained simulation state.
    """
    if not isinstance(config, CinematicConfig):
        raise TypeError("config must be a CinematicConfig")
    time_s = _finite_time(time_s)
    current, history = _interaction_placements(config, time_s, placements)
    phase = _phase(time_s, config.fluid_period_s)
    current_capsules = [_placement_capsules(placement, phase) for placement in current]
    current_bounds = [_capsule_bounds(capsules) for capsules in current_capsules]
    history_phases = tuple(_phase(time_s - lag_s, config.fluid_period_s) for lag_s in (0.2, 0.4))
    history_capsules = [
        [_placement_capsules(placement, past_phase) for placement in past]
        for past, past_phase in zip(history, history_phases)
    ]
    history_bounds = [[_capsule_bounds(capsules) for capsules in past] for past in history_capsules]
    offsets = [[0.0, 0.0] for _ in current]
    width_changes = [0.0 for _ in current]
    contacts = []
    for first in range(3):
        for second in range(first + 1, 4):
            if _bounds_can_interact(current_bounds[first], current_bounds[second]):
                proximity, point_first, point_second, capsule_radius = _capsule_pair_contact(
                    current_capsules[first], current_capsules[second]
                )
            else:
                proximity, point_first, point_second, capsule_radius = 0.0, (0.0, 0.0), (0.0, 0.0), 0.0
            if proximity == 0.0:
                continue
            lagged = [
                _capsule_pair_proximity(past[first], past[second])
                if _bounds_can_interact(bounds[first], bounds[second]) else 0.0
                for past, bounds in zip(history_capsules, history_bounds)
            ]
            lagged_proximity = (proximity + lagged[0] + lagged[1]) / 3.0
            velocity_first = (
                (current[first][0] - history[0][first][0]) / 0.2,
                (current[first][1] - history[0][first][1]) / 0.2,
            )
            velocity_second = (
                (current[second][0] - history[0][second][0]) / 0.2,
                (current[second][1] - history[0][second][1]) / 0.2,
            )
            center_dx = current[second][0] - current[first][0]
            center_dy = current[second][1] - current[first][1]
            center_distance = math.hypot(center_dx, center_dy) or 1.0
            outward_x, outward_y = center_dx / center_distance, center_dy / center_distance
            relative_speed = ((velocity_first[0] - velocity_second[0]) * outward_x
                              + (velocity_first[1] - velocity_second[1]) * outward_y)
            center_approach = max(0.0, min(1.0, -relative_speed / 100.0))
            capsule_approach = max(0.0, min(1.0, 3.0 * (proximity - lagged[0])))
            approach = max(center_approach, capsule_approach)
            strength = proximity * (0.50 + 0.50 * lagged_proximity)
            if strength == 0.0:
                continue
            slow = 0.050 * strength * (0.45 + 0.55 * approach)
            lateral = 1.35 * strength * (0.65 + 0.35 * approach)
            side_x, side_y = -outward_y, outward_x
            offsets[first][0] += -slow * velocity_first[0] + lateral * side_x
            offsets[first][1] += -slow * velocity_first[1] + lateral * side_y
            offsets[second][0] += -slow * velocity_second[0] - lateral * side_x
            offsets[second][1] += -slow * velocity_second[1] - lateral * side_y
            exchange = 0.040 * strength * math.sin(phase + current[first][5] - current[second][5])
            width_changes[first] += exchange
            width_changes[second] -= exchange
            contacts.append(_PairContact(
                first, second, strength, proximity, lagged_proximity, approach,
                point_first, point_second, max(4.0, min(15.0, 0.23 * capsule_radius)),
            ))
    responsive = tuple(
        (center_x + offsets[index][0], center_y + offsets[index][1], angle, half_length,
         half_width * max(0.92, min(1.08, 1.0 + width_changes[index])), phase_offset)
        for index, (center_x, center_y, angle, half_length, half_width, phase_offset) in enumerate(current)
    )
    return _InteractionGeometry(responsive, tuple(contacts))


def _add_local_neck(np, body_fields, contact: _PairContact):
    """Blend one local bridge into its two involved body fields, never a global tail."""
    first_x, first_y = contact.point_first
    second_x, second_y = contact.point_second
    dx, dy = second_x - first_x, second_y - first_y
    distance = math.hypot(dx, dy)
    cosine, sine = (dx / distance, dy / distance) if distance > 1e-6 else (1.0, 0.0)
    center_x, center_y = (first_x + second_x) * 0.5, (first_y + second_y) * 0.5
    half_length, radius = max(5.0, distance * 0.5 + 3.5), contact.neck_radius
    x_radius = abs(cosine) * half_length + abs(sine) * radius + 1.0
    y_radius = abs(sine) * half_length + abs(cosine) * radius + 1.0
    left, right = max(0, math.floor(center_x - x_radius)), min(_FIELD_W, math.ceil(center_x + x_radius) + 1)
    top, bottom = max(0, math.floor(center_y - y_radius)), min(_FIELD_H, math.ceil(center_y + y_radius) + 1)
    if left >= right or top >= bottom:
        return
    yy, xx = _static_coordinates(np, _FIELD_H, _FIELD_W)
    yy, xx = yy[top:bottom, left:right], xx[top:bottom, left:right]
    longitudinal = (xx - center_x) * cosine + (yy - center_y) * sine
    transverse = -(xx - center_x) * sine + (yy - center_y) * cosine
    neck = contact.strength * np.maximum(0.0, 1.0 - (longitudinal / half_length) ** 2 - (transverse / radius) ** 2)
    first_share = neck * (0.72 - 0.20 * longitudinal / half_length)
    second_share = neck * (0.72 + 0.20 * longitudinal / half_length)
    body_fields[contact.first][top:bottom, left:right] = np.maximum(body_fields[contact.first][top:bottom, left:right], first_share)
    body_fields[contact.second][top:bottom, left:right] = np.maximum(body_fields[contact.second][top:bottom, left:right], second_share)


def _body_fields(np, xx, yy, config: CinematicConfig, time_s: float, *, intrinsic_time_s: float | None = None):
    """Build the four real interaction-aware fields that are MAX-composited by the renderer."""
    geometry = _interaction_geometry(config, time_s)
    phase = _phase(time_s if intrinsic_time_s is None else intrinsic_time_s, config.fluid_period_s)
    fields = []
    for center_x, center_y, angle, half_length, half_width, phase_offset in geometry.placements:
        cosine, sine = math.cos(angle), math.sin(angle)
        longitudinal = (xx - center_x) * cosine + (yy - center_y) * sine
        transverse = -(xx - center_x) * sine + (yy - center_y) * cosine
        fields.append(_canonical_body_field(np, longitudinal, transverse, phase + phase_offset, half_length, half_width))
    for contact in geometry.contacts:
        _add_local_neck(np, fields, contact)
    for field in fields:
        np.clip(field, 0.0, 1.0, out=field)
    return fields


def _height_surface_shade(np, field):
    """Light the low-resolution fluid height surface without altering its silhouette."""
    height = np.sqrt(field)
    gradient_y, gradient_x = np.gradient(height)
    # Clamp one-pixel edge slopes before normalizing so the hard silhouette cannot
    # turn into a thin directional rim when the field is upscaled.
    slope_x = np.clip(2.2 * gradient_x, -0.45, 0.45)
    slope_y = np.clip(2.2 * gradient_y, -0.45, 0.45)
    normal_length = np.sqrt(slope_x * slope_x + slope_y * slope_y + 1.0)
    normal_x, normal_y, normal_z = -slope_x / normal_length, -slope_y / normal_length, 1.0 / normal_length
    key = np.maximum(0.0, -0.78 * normal_x - 0.50 * normal_y + 0.38 * normal_z)
    half_key = np.maximum(0.0, -0.43 * normal_x - 0.28 * normal_y + 0.86 * normal_z)
    depth = field ** 1.15
    diffuse = 0.14 + 0.86 * key
    broad_highlight = 3.0 * depth * half_key ** 5
    return np.clip(1.0 + 132.0 * depth * diffuse + broad_highlight, 1.0, 60.0).astype(np.uint8)


def field_rgba(config: CinematicConfig, time_s: float):
    """Return the production 400x180 lava RGBA field with fluid deformation and travel."""
    if not isinstance(config, CinematicConfig):
        raise TypeError("config must be a CinematicConfig")
    time_s = _finite_time(time_s)
    np, *_ = _render_dependencies()
    yy, xx = _static_coordinates(np, _FIELD_H, _FIELD_W)
    # Keep the production composite bounded to four locally modified body fields.
    # Pair response is a deterministic kinematic approximation, not a fluid solver.
    field = np.maximum.reduce(_body_fields(np, xx, yy, config, time_s))
    active = field > 0.0
    shade = _height_surface_shade(np, field)
    lava = _hsv_rgb(lava_hue(time_s, config.hue_period_s), 0.80, 1.0)
    rgba = np.zeros((_FIELD_H, _FIELD_W, 4), dtype=np.uint8)
    for channel, amount in enumerate(lava):
        rgba[:, :, channel] = np.where(active, np.minimum(64, shade.astype(np.uint16) * amount // 255), 0)
    rgba[:, :, 3] = 255
    return rgba


def _image_from_rgba(QImage, rgba):
    height, width = rgba.shape[:2]
    return QImage(rgba.data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()


def _bounded_cache_put(cache, key, value, limit: int):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)
    return value


def _build_coordinate_grid(np, height: int, width: int):
    """Create one exact mgrid pair, then freeze it before any production use."""
    yy, xx = np.mgrid[0:height, 0:width]
    yy.setflags(write=False)
    xx.setflags(write=False)
    return yy, xx


def _static_coordinates(np, height: int, width: int):
    key = (height, width)
    coordinates = _COORDINATE_CACHE.get(key)
    if coordinates is None:
        coordinates = _bounded_cache_put(
            _COORDINATE_CACHE, key, _build_coordinate_grid(np, height, width), _COORDINATE_CACHE_MAX
        )
    else:
        _COORDINATE_CACHE.move_to_end(key)
    return coordinates


def _create_font(QFont, family: str, pixel_size: int, bold: bool = False):
    """Create a mutable QFont once; callers receive detached copies below."""
    font = QFont(family)
    font.setPixelSize(pixel_size)
    font.setBold(bold)
    return font


def _font(QFont, family: str, pixel_size: int, bold: bool = False):
    """Return a mutable copy of a bounded, Qt-initialized font prototype."""
    _ensure_qt_application()
    key = (family, pixel_size, bold)
    prototype = _FONT_CACHE.get(key)
    if prototype is None:
        prototype = _bounded_cache_put(
            _FONT_CACHE, key, _create_font(QFont, family, pixel_size, bold), _FONT_CACHE_MAX
        )
    else:
        _FONT_CACHE.move_to_end(key)
    return QFont(prototype)


def _clear_static_render_caches_for_tests() -> None:
    """Reset private caches for isolated renderer contracts; production never needs this."""
    _COORDINATE_CACHE.clear()
    _FONT_CACHE.clear()


def _static_render_cache_info() -> dict[str, Any]:
    """Expose bounded cache accounting to renderer contracts without exposing entries."""
    coordinate_bytes = sum(grid.nbytes for coordinates in _COORDINATE_CACHE.values() for grid in coordinates)
    return {
        "coordinates": len(_COORDINATE_CACHE),
        "fonts": len(_FONT_CACHE),
        "static_bytes": coordinate_bytes,
        "limits": {
            "coordinates": _COORDINATE_CACHE_MAX,
            "fonts": _FONT_CACHE_MAX,
        },
    }


def _rect(metrics, text: str, x: float, baseline: float):
    raw = metrics.boundingRect(text)
    return raw.translated(x - raw.x(), baseline)


def _fit_digit_font(QFont, QFontMetricsF, text: str, x: float, column_right: float, unit_text: str):
    for pixel_size in range(218, 95, -2):
        font = _font(QFont, "DejaVu Sans Mono", pixel_size, True)
        advance = QFontMetricsF(font).horizontalAdvance(text)
        unit = _font(QFont, "DejaVu Sans", 38)
        if advance + (QFontMetricsF(unit).horizontalAdvance(unit_text) + 16 if unit_text else 0) <= column_right - x:
            return font
    return _font(QFont, "DejaVu Sans Mono", 94, True)


def temperature_layout(temp: float | None, x: float, column_right: float, *, shift: float = 0.0):
    """Measured production rectangles/fonts for a CPU/GPU column; used by the renderer."""
    _ensure_qt_application()
    _, _, _, QFont, QFontMetricsF, *_ = _render_dependencies()
    text = _text_number(temp)
    unit_text = "°C" if temp is not None else ""
    x += shift
    font = _fit_digit_font(QFont, QFontMetricsF, text, x, column_right + shift, unit_text)
    number_metrics = QFontMetricsF(font)
    number = _rect(number_metrics, text, x, 424.0)
    label_font = _font(QFont, "DejaVu Sans", 19, True)
    label = _rect(QFontMetricsF(label_font), "CPU", x, 202.0)
    unit_font = _font(QFont, "DejaVu Sans", 38)
    unit_x = x + number_metrics.horizontalAdvance(text) + 16.0
    unit = _rect(QFontMetricsF(unit_font), unit_text, unit_x, 284.0) if unit_text else number.adjusted(0, 0, 0, 0)
    if not unit_text:
        unit.setWidth(0.0)
        unit.setHeight(0.0)
    return {"number": number, "label": label, "unit": unit, "number_font": font,
            "label_font": label_font, "unit_font": unit_font, "text": text, "unit_text": unit_text}


def _title_text(Qt, QFont, QFontMetricsF, hostname: str):
    font = _font(QFont, "DejaVu Sans", 84, True)
    text = str(hostname or "MASTER").upper()
    return font, QFontMetricsF(font).elidedText(text, Qt.TextElideMode.ElideRight, _TITLE_W - 4)


def _qimage_array(np, image):
    return np.frombuffer(image.bits(), dtype=np.uint8).reshape(image.height(), image.width(), 4)


def _title_liquid_and_shading(np, yy, xx, phase: float):
    """Build title fill masks from row/column views of the cached integer grids."""
    x = xx[:1, :]
    y = yy[:, :1]
    surface = 57.0 + 9.0 * np.sin(x / 59.0 + phase) + 5.0 * np.sin(x / 21.0 - 2.0 * phase)
    tongues = 13.0 * np.maximum(0.0, np.sin(x / 37.0 + phase)) ** 3
    pockets = ((x - 105.0 - 28.0 * math.sin(phase)) / 17.0) ** 2 + ((y - 42.0) / 10.0) ** 2 < 1.0
    pockets |= ((x - 355.0 - 20.0 * math.cos(phase)) / 14.0) ** 2 + ((y - 49.0) / 8.0) ** 2 < 1.0
    liquid = (y >= surface - tongues) | pockets
    shading = np.clip(0.48 + 0.35 * np.sin(x / 24.0 + phase) + 0.17 * np.cos(y / 9.0 - phase), 0.25, 1.0)
    return liquid, shading


def render_title_layer(time_s: float, *, config: CinematicConfig = CinematicConfig(), hostname: str = "MASTER"):
    """Build the real transparent title/mask layers independently of the lava background."""
    if not isinstance(config, CinematicConfig):
        raise TypeError("config must be a CinematicConfig")
    time_s = _finite_time(time_s)
    _ensure_qt_application()
    np, Qt, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen = _render_dependencies()
    font, text = _title_text(Qt, QFont, QFontMetricsF, hostname)
    # Qt's rasterized title mask evolves during a running app. Cache only the font
    # prototype; rebuild glyph path/mask/alpha every frame to preserve exact output.
    path = QPainterPath()
    path.addText(0.0, 82.0, font, text)
    mask = QImage(_TITLE_W, _TITLE_H, QImage.Format.Format_RGBA8888)
    mask.fill(Qt.GlobalColor.transparent)
    painter = QPainter(mask)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Qt.GlobalColor.white)
    painter.drawPath(path)
    painter.end()
    alpha = _qimage_array(np, mask)[:, :, 3].copy()

    yy, xx = _static_coordinates(np, _TITLE_H, _TITLE_W)
    phase = _phase(time_s, config.title_period_s)
    liquid, shading = _title_liquid_and_shading(np, yy, xx, phase)
    rgb = np.zeros((_TITLE_H, _TITLE_W, 4), dtype=np.uint8)
    rgb[:, :, :3] = (10, 6, 3)
    hue = foreground_hue(time_s, config.hue_period_s)
    amber = _hsv_rgb(hue, 0.65, 0.86)
    for channel, amount in enumerate(amber):
        rgb[:, :, channel] = np.where(liquid, np.asarray(amount * shading, dtype=np.uint8), rgb[:, :, channel])
    rgb[:, :, 3] = alpha
    layer = _image_from_rgba(QImage, rgb)
    painter = QPainter(layer)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
    painter.setClipPath(path)
    outline = _color(QColor, _hsv_rgb(hue, 0.58, 0.76), 210)
    pen = QPen(outline)
    pen.setWidthF(1.25)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.end()
    pixels = _qimage_array(np, layer)
    pixels[:, :, 3] = alpha  # A clipped layer must remain exactly transparent outside glyphs.
    pixels[alpha == 0, :3] = 0
    return layer, mask, path.boundingRect()


def _draw_text(painter, QFont, text: str, x: float, baseline: float, pixel_size: int, color, bold: bool = False):
    painter.setFont(_font(QFont, "DejaVu Sans", pixel_size, bold))
    painter.setPen(color)
    painter.drawText(x, baseline, text)


def power_layout(power_w: float | None, x: float, *, shift: float = 0.0):
    """Measure the compact power text against this column's visible graph edge."""
    _ensure_qt_application()
    _, _, _, QFont, QFontMetricsF, *_ = _render_dependencies()
    text = format_power(power_w)
    font = _font(QFont, "DejaVu Sans", 22)
    metrics = QFontMetricsF(font)
    glyph_bounds = metrics.boundingRect(text)
    advance = metrics.horizontalAdvance(text)
    graph_right = x + 578.0 + shift
    # The advance is a logical position; correct it by the measured right bearing
    # so the actual glyph bound, not an assumed text width, reaches graph_right.
    draw_x = graph_right - advance - (glyph_bounds.right() - advance)
    return {
        "text": text,
        "font": font,
        "x": draw_x,
        "baseline": 500.0,
        "graph_right": graph_right,
        "advance": advance,
        "glyph_bounds": glyph_bounds.translated(draw_x, 500.0),
    }


def _draw_temperature(painter, QColor, QFont, QPen, temp: float | None, usage: float | None,
                      label: str, x: float, column_right: float, shift: float, accent, pale,
                      *, digit=None, power_w: float | None = None):
    layout = temperature_layout(temp, x, column_right, shift=shift)
    painter.setFont(layout["label_font"])
    painter.setPen(accent)
    painter.drawText(x + shift, 202.0, label)
    painter.setFont(layout["number_font"])
    painter.setPen(pale if digit is None else digit)
    painter.drawText(x + shift, 424.0, layout["text"])
    if layout["unit_text"]:
        painter.setFont(layout["unit_font"])
        painter.setPen(accent)
        painter.drawText(layout["unit"].x(), 284.0, layout["unit_text"])
    usage_text = f"Uso {_text_number(usage)} %" if usage is not None else "Uso N/A"
    _draw_text(painter, QFont, usage_text, x + shift, 500.0, 22, accent)
    power = power_layout(power_w, x, shift=shift)
    _draw_text(painter, QFont, power["text"], power["x"], power["baseline"], 22, pale)


def _draw_graph(painter, QFont, QPen, view, now: float | None, x: float, shift: float, accent):
    segments, bounds = view
    pen = QPen(accent)
    pen.setWidthF(1.8)
    painter.setPen(pen)
    if now is not None and bounds is not None:
        for (left_t, left_v), (right_t, right_v) in segments:
            left_x = x + shift + _GRAPH_WIDTH * (left_t - (now - 300.0)) / 300.0
            right_x = x + shift + _GRAPH_WIDTH * (right_t - (now - 300.0)) / 300.0
            painter.drawLine(left_x, _graph_y(left_v, bounds), right_x, _graph_y(right_v, bounds))
    label = "Auto · N/A · 5 min" if bounds is None else f"Auto · {bounds[0]:.0f}–{bounds[1]:.0f} °C · 5 min"
    _draw_text(painter, QFont, label, x + shift, 612.0, 11, accent)


def _ram_text(snapshot: Any) -> tuple[str, str]:
    used, total, percent = _value(snapshot, "ram", "used_mb"), _value(snapshot, "ram", "total_mb"), _value(snapshot, "ram", "percent")
    if used is None or total is None or used < 0 or total <= 0:
        return "N/A", "N/A" if percent is None else f"{_text_number(percent)} %"
    return f"{used / 1024.0:.1f} / {total / 1024.0:.0f} GB".replace(".", ","), "N/A" if percent is None else f"{_text_number(percent)} %"


def _ui_shift(time_s: float, config: CinematicConfig) -> float:
    return 6.0 * math.sin(_phase(time_s, config.title_period_s))


def _synthetic_snapshot():
    cpu_history = tuple(SimpleNamespace(elapsed_s=float(t), value=47.0 + 7.0 * math.sin(t / 47.0)) for t in range(301))
    gpu_history = tuple(SimpleNamespace(elapsed_s=float(t), value=58.0 + 5.0 * math.cos(t / 39.0)) for t in range(301))
    return SimpleNamespace(
        cpu=SimpleNamespace(temp=58.0, usage=37.0, power_w=75.0),
        gpu=SimpleNamespace(temp=63.0, usage=71.0, power_w=31.0),
        ram=SimpleNamespace(used_mb=12492.8, total_mb=32768.0, percent=38.0),
        cpu_history=cpu_history, gpu_history=gpu_history, history_now_s=300.0, demo=True,
    )


def render_cinematic_image(snapshot: Any, time_s: float, *, config: CinematicConfig = CinematicConfig(),
                            hostname: str = "MASTER", field_renderer: Callable[[CinematicConfig, float], Any] | None = None) -> "QImage":
    """Render one frame from stable snapshot attrs without retaining or changing snapshot data."""
    if not isinstance(config, CinematicConfig):
        raise TypeError("config must be a CinematicConfig")
    time_s = _finite_time(time_s)
    if field_renderer is not None and not callable(field_renderer):
        raise TypeError("field_renderer must be callable or None")
    _ensure_qt_application()
    np, Qt, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen = _render_dependencies()
    image = QImage(config.width, config.height, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.black)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    field = _image_from_rgba(QImage, field_rgba(config, time_s) if field_renderer is None else field_renderer(config, time_s))
    painter.drawImage(0, 0, field.scaled(config.width, config.height, Qt.AspectRatioMode.IgnoreAspectRatio,
                                         Qt.TransformationMode.FastTransformation))
    accent_rgb, pale_rgb = _palette(time_s, config)
    accent, pale = _color(QColor, accent_rgb), _color(QColor, pale_rgb)
    temperature_digit = _color(QColor, _temperature_rgb(time_s, config))
    shift = _ui_shift(time_s, config)
    title, _, _ = render_title_layer(time_s, config=config, hostname=hostname)
    painter.drawImage(round(78.0 + shift), 34, title)
    _draw_temperature(painter, QColor, QFont, QPen, _value(snapshot, "cpu", "temp"), _value(snapshot, "cpu", "usage"),
                      "CPU", 80.0, 760.0, shift, accent, pale, digit=temperature_digit,
                      power_w=_value(snapshot, "cpu", "power_w"))
    _draw_temperature(painter, QColor, QFont, QPen, _value(snapshot, "gpu", "temp"), _value(snapshot, "gpu", "usage"),
                      "GPU", 880.0, 1528.0, shift, accent, pale, digit=temperature_digit,
                      power_w=_value(snapshot, "gpu", "power_w"))
    now = _number(_attr(snapshot, "history_now_s"))
    cpu_view = graph_view(_attr(snapshot, "cpu_history", ()), now)
    gpu_view = graph_view(_attr(snapshot, "gpu_history", ()), now)
    _draw_graph(painter, QFont, QPen, cpu_view, now, 80.0, shift, accent)
    _draw_graph(painter, QFont, QPen, gpu_view, now, 880.0, shift, accent)
    ram, percent = _ram_text(snapshot)
    _draw_text(painter, QFont, "RAM", 80.0 + shift, 675.0, 20, accent, True)
    _draw_text(painter, QFont, ram, 150.0 + shift, 675.0, 23, pale)
    _draw_text(painter, QFont, percent, 1450.0 + shift, 675.0, 23, accent, True)
    if bool(_attr(snapshot, "demo", False)):
        _draw_text(painter, QFont, "DEMO", 1395.0 + shift, 76.0, 15, accent, True)
    painter.end()
    return image


def write_cinematic_preview(output: str | Path, *, time_s: float = 0.0, hostname: str | None = None) -> None:
    """Write a synthetic-DEMO PNG only to a new path under an existing parent directory."""
    _finite_time(time_s)
    path = Path(output)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing preview: {path}")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"preview parent does not exist: {path.parent}")
    image = render_cinematic_image(_synthetic_snapshot(), time_s, hostname=hostname or socket.gethostname())
    if not image.save(str(path), "PNG"):
        raise OSError(f"could not write PNG preview: {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write one synthetic offline cinematic PNG preview.")
    parser.add_argument("--preview", required=True, metavar="PATH")
    parser.add_argument("--time", type=float, default=0.0, metavar="SECONDS")
    args = parser.parse_args(argv)
    write_cinematic_preview(args.preview, time_s=args.time)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
