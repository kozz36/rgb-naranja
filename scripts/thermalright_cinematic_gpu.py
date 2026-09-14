"""Offline-only OpenGL sidecar for investigating the cinematic fluid field.

This is intentionally not imported by the renderer, CLI, service, or exporter.  Keep a
GpuFluidSidecar open to amortize context and shader setup; ``field_rgba`` is the
small convenience API for one-off offline comparisons.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import sys
import threading
from typing import Any


_FIELD_W, _FIELD_H = 400, 180
_GL_TRIANGLES = 0x0004
_GL_TEXTURE0 = 0x84C0
_GL_TEXTURE_2D = 0x0DE1
_GL_RGBA8 = 0x8058
_GL_R32F = 0x822E
_GL_DITHER = 0x0BD0
_GL_FRAMEBUFFER_SRGB = 0x8DB9
_GL_VENDOR = 0x1F00
_GL_RENDERER = 0x1F01
_GL_VERSION = 0x1F02
_REFERENCE: Any | None = None


class GpuUnavailableError(RuntimeError):
    """Raised when the explicit offline sidecar cannot get a hardware GL context."""


@dataclass(frozen=True)
class GpuInfo:
    vendor: str
    renderer: str
    version: str


def _reference():
    """Load the CPU renderer only when a request needs its public geometry helpers."""
    global _REFERENCE
    if _REFERENCE is None:
        scripts_dir = str(Path(__file__).resolve().parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        _REFERENCE = importlib.import_module("thermalright_cinematic")
    return _REFERENCE


def _validate_request(config: Any, time_s: Any) -> tuple[Any, float]:
    reference = _reference()
    if not isinstance(config, reference.CinematicConfig):
        raise TypeError("config must be a CinematicConfig")
    return reference, reference._finite_time(time_s)


def _gl_string(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


_VERTEX_SHADER = """#version 330 core
void main() {
    const vec2 vertices[3] = vec2[3](vec2(-1.0, -1.0), vec2(3.0, -1.0), vec2(-1.0, 3.0));
    gl_Position = vec4(vertices[gl_VertexID], 0.0, 1.0);
}
"""

_FIELD_FRAGMENT_SHADER = """#version 330 core
layout(location = 0) out float out_field;
uniform vec4 u_body[4];
uniform vec2 u_shape[4];
uniform vec4 u_contact_meta[6];
uniform vec4 u_contact_points[6];
uniform vec2 u_phase;
uniform int u_contact_count;

float body_field(vec4 body, vec2 shape, float phase, vec2 point) {
    float cosine = cos(body.z), sine = sin(body.z);
    float longitudinal = (point.x - body.x) * cosine + (point.y - body.y) * sine;
    float transverse = -(point.x - body.x) * sine + (point.y - body.y) * cosine;
    float u = longitudinal / body.w;
    float edge = clamp(1.0 - u * u, 0.0, 1.0);
    float body_phase = phase + shape.y;
    float centerline = shape.x * edge * (
        0.456 * sin(3.141592653589793 * u + body_phase)
        + 0.188 * sin(6.283185307179586 * u - 2.0 * body_phase)
    );
    float transport = sin(body_phase - 0.60);
    float left_lobe = -0.58 + 0.08 * sin(body_phase + 0.35);
    float right_lobe = 0.58 + 0.08 * sin(body_phase - 0.50);
    float neck = 0.16 * sin(body_phase + 0.80);
    float radius = shape.x * (
        1.0
        + (0.483 + 0.268 * transport) * exp(-pow((u - left_lobe) / 0.24, 2.0))
        + (0.483 - 0.268 * transport) * exp(-pow((u - right_lobe) / 0.24, 2.0))
        - (0.456 + 0.134 * cos(body_phase - 0.30)) * exp(-pow((u - neck) / 0.22, 2.0))
    );
    return max(0.0, 1.0 - u * u - pow((transverse - centerline) / radius, 2.0));
}

void apply_contact(vec4 meta, vec4 points, vec2 pixel, inout float first, inout float second,
                   inout float third, inout float fourth) {
    vec2 point_first = points.xy, point_second = points.zw;
    vec2 delta = point_second - point_first;
    float distance = length(delta);
    vec2 direction = distance > 0.000001 ? delta / distance : vec2(1.0, 0.0);
    vec2 center = (point_first + point_second) * 0.5;
    float half_length = max(5.0, distance * 0.5 + 3.5);
    float longitudinal = dot(pixel - center, direction);
    float transverse = dot(pixel - center, vec2(-direction.y, direction.x));
    float bridge = meta.z * max(0.0, 1.0 - pow(longitudinal / half_length, 2.0)
                                      - pow(transverse / meta.w, 2.0));
    float first_share = bridge * (0.72 - 0.20 * longitudinal / half_length);
    float second_share = bridge * (0.72 + 0.20 * longitudinal / half_length);
    int first_index = int(meta.x + 0.5), second_index = int(meta.y + 0.5);
    if (first_index == 0) first = max(first, first_share);
    if (first_index == 1) second = max(second, first_share);
    if (first_index == 2) third = max(third, first_share);
    if (first_index == 3) fourth = max(fourth, first_share);
    if (second_index == 0) first = max(first, second_share);
    if (second_index == 1) second = max(second, second_share);
    if (second_index == 2) third = max(third, second_share);
    if (second_index == 3) fourth = max(fourth, second_share);
}

void main() {
    vec2 pixel = vec2(floor(gl_FragCoord.x), 179.0 - floor(gl_FragCoord.y));
    float first = body_field(u_body[0], u_shape[0], u_phase.x, pixel);
    float second = body_field(u_body[1], u_shape[1], u_phase.x, pixel);
    float third = body_field(u_body[2], u_shape[2], u_phase.x, pixel);
    float fourth = body_field(u_body[3], u_shape[3], u_phase.x, pixel);
    for (int index = 0; index < 6; ++index) {
        if (index >= u_contact_count) break;
        apply_contact(u_contact_meta[index], u_contact_points[index], pixel, first, second, third, fourth);
    }
    out_field = clamp(max(max(first, second), max(third, fourth)), 0.0, 1.0);
}
"""

_COLOR_FRAGMENT_SHADER = """#version 330 core
layout(location = 0) out vec4 out_rgba;
uniform sampler2D u_height;
uniform vec3 u_rgb;

float height_at(ivec2 point) { return sqrt(max(texelFetch(u_height, point, 0).r, 0.0)); }

void main() {
    ivec2 pixel = ivec2(floor(gl_FragCoord.xy));
    float field = texelFetch(u_height, pixel, 0).r;
    if (field <= 0.0) { out_rgba = vec4(0.0, 0.0, 0.0, 1.0); return; }
    float height = sqrt(max(field, 0.0));
    float gradient_x = pixel.x == 0 ? height_at(pixel + ivec2(1, 0)) - height
                     : pixel.x == 399 ? height - height_at(pixel - ivec2(1, 0))
                     : (height_at(pixel + ivec2(1, 0)) - height_at(pixel - ivec2(1, 0))) * 0.5;
    int logical_y = 179 - pixel.y;
    float gradient_y = logical_y == 0 ? height_at(pixel - ivec2(0, 1)) - height
                     : logical_y == 179 ? height - height_at(pixel + ivec2(0, 1))
                     : (height_at(pixel - ivec2(0, 1)) - height_at(pixel + ivec2(0, 1))) * 0.5;
    float slope_x = clamp(2.2 * gradient_x, -0.45, 0.45);
    float slope_y = clamp(2.2 * gradient_y, -0.45, 0.45);
    float normal_length = sqrt(slope_x * slope_x + slope_y * slope_y + 1.0);
    float normal_x = -slope_x / normal_length, normal_y = -slope_y / normal_length, normal_z = 1.0 / normal_length;
    float key = max(0.0, -0.78 * normal_x - 0.50 * normal_y + 0.38 * normal_z);
    float half_key = max(0.0, -0.43 * normal_x - 0.28 * normal_y + 0.86 * normal_z);
    float depth = pow(field, 1.15);
    float shade_value = clamp(1.0 + 132.0 * depth * (0.14 + 0.86 * key) + 3.0 * depth * pow(half_key, 5.0), 1.0, 60.0);
    int shade = int(shade_value);
    ivec3 amount = ivec3(u_rgb + 0.5);
    ivec3 color = min(ivec3(64), (ivec3(shade) * amount) / 255);
    out_rgba = vec4(vec3(color) / 255.0, 1.0);
}
"""


class GpuFluidSidecar:
    """A main-thread, explicit-lifetime hardware OpenGL renderer for the 400x180 field."""

    def __init__(self) -> None:
        self._thread_id = threading.get_ident()
        self._closed = False
        self._context = self._surface = self._height_fbo = self._color_fbo = None
        self._field_program = self._color_program = self._vao = self._functions = None
        self._application = None
        try:
            self._initialize()
        except Exception as error:
            try:
                self.close()
            except Exception as cleanup_error:
                raise error from cleanup_error
            raise

    def _initialize(self) -> None:
        try:
            import numpy as np
            from PySide6 import QtCore, QtGui, QtOpenGL
        except ImportError as error:
            raise GpuUnavailableError("numpy and PySide6 are required for the offline GPU sidecar") from error
        self._np, self._QtCore, self._QtGui, self._QtOpenGL = np, QtCore, QtGui, QtOpenGL
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        application = QtGui.QGuiApplication.instance()
        self._application = application or QtGui.QGuiApplication([])
        surface_format = QtGui.QSurfaceFormat()
        surface_format.setRenderableType(QtGui.QSurfaceFormat.RenderableType.OpenGL)
        surface_format.setVersion(3, 3)
        surface_format.setProfile(QtGui.QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        self._surface = QtGui.QOffscreenSurface()
        self._surface.setFormat(surface_format)
        self._surface.create()
        self._context = QtGui.QOpenGLContext()
        self._context.setFormat(surface_format)
        if not self._surface.isValid() or not self._context.create() or not self._context.makeCurrent(self._surface):
            raise GpuUnavailableError("could not create a current Qt offscreen OpenGL 3.3 context")
        version_profile = QtOpenGL.QOpenGLVersionProfile()
        version_profile.setVersion(3, 3)
        version_profile.setProfile(QtGui.QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        self._functions = QtOpenGL.QOpenGLVersionFunctionsFactory.get(version_profile, self._context)
        if self._functions is None or not self._functions.initializeOpenGLFunctions():
            raise GpuUnavailableError("Qt could not initialize OpenGL 3.3 core functions")
        self.info = GpuInfo(*(_gl_string(self._functions.glGetString(token)) for token in (_GL_VENDOR, _GL_RENDERER, _GL_VERSION)))
        if any(marker in self.info.renderer.lower() for marker in ("llvmpipe", "softpipe", "software")):
            raise GpuUnavailableError(f"refusing software OpenGL renderer: {self.info.renderer}")
        self._functions.glDisable(_GL_DITHER)
        self._functions.glDisable(_GL_FRAMEBUFFER_SRGB)
        self._height_fbo = self._fbo(_GL_R32F)
        self._color_fbo = self._fbo(_GL_RGBA8)
        self._vao = QtOpenGL.QOpenGLVertexArrayObject()
        if not self._vao.create():
            raise GpuUnavailableError("could not create the core-profile fullscreen VAO")
        self._field_program = self._program(_FIELD_FRAGMENT_SHADER)
        self._color_program = self._program(_COLOR_FRAGMENT_SHADER)

    def _fbo(self, texture_format: int):
        fbo_format = self._QtOpenGL.QOpenGLFramebufferObjectFormat()
        fbo_format.setAttachment(self._QtOpenGL.QOpenGLFramebufferObject.Attachment.NoAttachment)
        fbo_format.setTextureTarget(_GL_TEXTURE_2D)
        fbo_format.setInternalTextureFormat(texture_format)
        fbo = self._QtOpenGL.QOpenGLFramebufferObject(_FIELD_W, _FIELD_H, fbo_format)
        if not fbo.isValid():
            raise GpuUnavailableError(f"could not create {texture_format:#x} offscreen framebuffer")
        return fbo

    def _program(self, fragment_source: str):
        program = self._QtOpenGL.QOpenGLShaderProgram()
        shader = self._QtOpenGL.QOpenGLShader.ShaderTypeBit
        if not program.addShaderFromSourceCode(shader.Vertex, _VERTEX_SHADER) or not program.addShaderFromSourceCode(shader.Fragment, fragment_source) or not program.link():
            raise GpuUnavailableError(f"could not compile fluid sidecar shader: {program.log()}")
        return program

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("GPU fluid sidecar is closed")
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("GPU fluid sidecar must be used and closed on its creating thread")
        if not self._context.makeCurrent(self._surface):
            raise GpuUnavailableError("could not make the GPU sidecar context current")

    @staticmethod
    def _uniform(program, name: str, value: Any) -> None:
        location = program.uniformLocation(name.encode())
        if location < 0:
            raise GpuUnavailableError(f"shader uniform is unavailable: {name}")
        program.setUniformValue(location, value)

    def _set_geometry_uniforms(self, reference, config: Any, time_s: float) -> None:
        geometry = reference._interaction_geometry(config, time_s)
        phase = reference._phase(time_s, config.fluid_period_s)
        vector2, vector4 = self._QtGui.QVector2D, self._QtGui.QVector4D
        self._uniform(self._field_program, "u_phase", vector2(phase, 0.0))
        self._uniform(self._field_program, "u_contact_count", len(geometry.contacts))
        for index, (x, y, angle, half_length, half_width, phase_offset) in enumerate(geometry.placements):
            self._uniform(self._field_program, f"u_body[{index}]", vector4(x, y, angle, half_length))
            self._uniform(self._field_program, f"u_shape[{index}]", vector2(half_width, phase_offset))
        for index, contact in enumerate(geometry.contacts):
            self._uniform(
                self._field_program, f"u_contact_meta[{index}]", vector4(contact.first, contact.second, contact.strength, contact.neck_radius)
            )
            self._uniform(
                self._field_program, f"u_contact_points[{index}]", vector4(*contact.point_first, *contact.point_second)
            )

    def _draw(self, fbo, program) -> None:
        fbo.bind()
        program.bind()
        self._vao.bind()
        self._functions.glViewport(0, 0, _FIELD_W, _FIELD_H)
        self._functions.glDrawArrays(_GL_TRIANGLES, 0, 3)
        self._vao.release()
        program.release()
        fbo.release()

    def render(self, config: Any, time_s: Any):
        """Return a tight 400x180 uint8 RGBA array after GPU completion and readback."""
        reference, time_s = _validate_request(config, time_s)
        self._check_open()
        self._functions.glDisable(_GL_DITHER)
        self._functions.glDisable(_GL_FRAMEBUFFER_SRGB)
        self._field_program.bind()
        self._set_geometry_uniforms(reference, config, time_s)
        self._field_program.release()
        self._draw(self._height_fbo, self._field_program)
        self._color_program.bind()
        self._functions.glActiveTexture(_GL_TEXTURE0)
        self._functions.glBindTexture(_GL_TEXTURE_2D, self._height_fbo.texture())
        self._uniform(self._color_program, "u_height", 0)
        rgb = reference._hsv_rgb(reference.lava_hue(time_s, config.hue_period_s), 0.80, 1.0)
        self._uniform(self._color_program, "u_rgb", self._QtGui.QVector3D(*rgb))
        self._color_program.release()
        self._draw(self._color_fbo, self._color_program)
        self._functions.glFinish()
        image = self._color_fbo.toImage().convertToFormat(self._QtGui.QImage.Format.Format_RGBA8888)
        data = self._np.frombuffer(image.constBits(), dtype=self._np.uint8, count=image.bytesPerLine() * image.height())
        return data.reshape(image.height(), image.bytesPerLine())[:, : _FIELD_W * 4].reshape(_FIELD_H, _FIELD_W, 4).copy()

    def close(self) -> None:
        if self._closed:
            return
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("GPU fluid sidecar must be closed on its creating thread")
        context, surface = self._context, self._surface
        if context is not None:
            if surface is None or not context.makeCurrent(surface):
                raise RuntimeError("could not make the GPU sidecar context current for close; resources and surface are retained for retry")
            if self._field_program is not None:
                self._field_program.removeAllShaders()
            if self._color_program is not None:
                self._color_program.removeAllShaders()
            if self._vao is not None:
                self._vao.destroy()
            if self._height_fbo is not None:
                self._height_fbo.release()
            if self._color_fbo is not None:
                self._color_fbo.release()
            # Drop every GL owner while its context is current on this thread.
            self._field_program = self._color_program = self._vao = None
            self._height_fbo = self._color_fbo = self._functions = None
            context.doneCurrent()
        elif any(resource is not None for resource in (self._field_program, self._color_program, self._vao, self._height_fbo, self._color_fbo)):
            raise RuntimeError("GPU fluid sidecar has GL resources without a context; resources and surface are retained for retry")
        if surface is not None:
            surface.destroy()
        self._context = self._surface = None
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def field_rgba(config: Any, time_s: Any):
    """Render one offline GPU field; use ``GpuFluidSidecar`` for repeated frames."""
    with GpuFluidSidecar() as sidecar:
        return sidecar.render(config, time_s)
