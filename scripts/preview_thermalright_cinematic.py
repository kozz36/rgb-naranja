"""Export deterministic synthetic cinematic frames to an offline DEMO WebM.

This tool never imports the live dashboard or hardware libraries. It renders the
cinematic renderer's synthetic snapshot at fixed timestamps, then streams raw RGBA
frames directly to ffmpeg; it does not create a PNG sequence or measure USB transport.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Callable

WIDTH = 1600
HEIGHT = 720
FRAME_BYTES = WIDTH * HEIGHT * 4
ALLOWED_FPS = (1, 6, 10, 24)
MAX_DURATION_S = 12.0
ENCODER_TIMEOUT_S = 30.0


class ExportError(RuntimeError):
    """The offline encoder could not produce a complete, newly-created WebM."""


def _duration_argument(value: str) -> float:
    try:
        duration_s = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite positive number no greater than 12") from exc
    if not math.isfinite(duration_s) or duration_s <= 0 or duration_s > MAX_DURATION_S:
        raise argparse.ArgumentTypeError("must be a finite positive number no greater than 12")
    return duration_s


def validate_export_request(output: str | Path, *, fps: int, duration_s: float) -> tuple[Path, int]:
    """Validate all user inputs before loading Qt/the renderer or starting ffmpeg."""
    path = Path(output)
    if path.suffix.lower() != ".webm":
        raise ValueError("output must have a .webm suffix")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"output parent does not exist: {path.parent}")
    if isinstance(fps, bool) or fps not in ALLOWED_FPS:
        raise ValueError(f"fps must be one of {ALLOWED_FPS}")
    if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
        raise ValueError("duration must be a finite positive number no greater than 12")
    duration = float(duration_s)
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_DURATION_S:
        raise ValueError("duration must be a finite positive number no greater than 12")
    exact_frames = duration * fps
    if not exact_frames.is_integer():
        raise ValueError("duration × fps must be an integer frame count")
    return path, int(exact_frames)


def _load_cinematic_renderer() -> Any:
    """Load the sibling renderer by path so both direct execution and importlib work."""
    source = Path(__file__).with_name("thermalright_cinematic.py")
    spec = importlib.util.spec_from_file_location("_thermalright_cinematic_export_renderer", source)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load cinematic renderer: {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fallback_demo_snapshot() -> SimpleNamespace:
    cpu_history = tuple(SimpleNamespace(elapsed_s=float(t), value=47.0 + 7.0 * math.sin(t / 47.0)) for t in range(301))
    gpu_history = tuple(SimpleNamespace(elapsed_s=float(t), value=58.0 + 5.0 * math.cos(t / 39.0)) for t in range(301))
    return SimpleNamespace(
        cpu=SimpleNamespace(temp=58.0, usage=37.0),
        gpu=SimpleNamespace(temp=63.0, usage=71.0),
        ram=SimpleNamespace(used_mb=12492.8, total_mb=32768.0, percent=38.0),
        cpu_history=cpu_history,
        gpu_history=gpu_history,
        history_now_s=300.0,
        demo=True,
    )


def _renderer_demo_snapshot(renderer: Any) -> Any:
    synthetic = getattr(renderer, "_synthetic_snapshot", None)
    snapshot = synthetic() if callable(synthetic) else _fallback_demo_snapshot()
    if not bool(getattr(snapshot, "demo", False)):
        raise ExportError("offline export requires a synthetic snapshot marked demo=True")
    return snapshot


def qimage_to_rgba(image: Any) -> bytes:
    """Validate and copy one real renderer QImage as tightly-packed 1600×720 RGBA."""
    try:
        from PySide6.QtGui import QImage
    except ImportError as exc:
        raise ImportError("PySide6 is required for cinematic WebM export") from exc
    if not isinstance(image, QImage):
        raise TypeError("cinematic renderer must return a QImage")
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    if (converted.width(), converted.height()) != (WIDTH, HEIGHT):
        raise ExportError("cinematic renderer returned the wrong frame dimensions")
    if converted.bytesPerLine() != WIDTH * 4 or converted.sizeInBytes() != FRAME_BYTES:
        raise ExportError("cinematic renderer returned a non-tight RGBA frame")
    raw = bytes(converted.bits())
    if len(raw) != FRAME_BYTES:
        raise ExportError("cinematic renderer RGBA byte count is invalid")
    return raw


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0


def _percentile95(values: list[float]) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)]


def _finite_metric(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ExportError(f"non-finite {name} measurement")
    return value


def _stderr_tail(handle: Any, limit: int = 4096) -> str:
    handle.seek(0, os.SEEK_END)
    handle.seek(max(0, handle.tell() - limit))
    return handle.read(limit).decode("utf-8", "replace").strip()


def _close_stdin(process: Any) -> None:
    try:
        process.stdin.close()
    except (BrokenPipeError, OSError, ValueError, AttributeError):
        pass


def _encoder_state(process: Any) -> str:
    pid = getattr(process, "pid", "unknown")
    try:
        status = process.poll()
    except (OSError, AttributeError):
        status = "unknown"
    return f"pid={pid} status={'running' if status is None else status}"


def _stop_encoder(process: Any, timeout_s: float) -> None:
    """Boundedly close, terminate, and reap the encoder or clearly report failure."""
    _close_stdin(process)
    try:
        if process.poll() is not None:
            return
    except (OSError, AttributeError):
        pass
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=timeout_s)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError as exc:
        raise ExportError(f"encoder cleanup could not wait for {_encoder_state(process)}") from exc
    try:
        process.kill()
    except OSError as exc:
        raise ExportError(f"encoder cleanup could not kill {_encoder_state(process)}") from exc
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise ExportError(f"encoder cleanup could not reap {_encoder_state(process)} after kill") from exc
    except OSError as exc:
        raise ExportError(f"encoder cleanup could not wait for {_encoder_state(process)} after kill") from exc


def _ffmpeg_command(output_fd: int, fps: int) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s:v", f"{WIDTH}x{HEIGHT}", "-r", str(fps), "-i", "-",
        "-an", "-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p", "-crf", "32", "-b:v", "0",
        "-deadline", "good", "-cpu-used", "4", "-threads", "2", "-f", "webm", f"pipe:{output_fd}",
    ]


def export_webm(
    output: str | Path,
    *,
    fps: int,
    duration_s: float,
    hostname: str,
    snapshot_factory: Callable[[], Any] | None = None,
    render_callback: Callable[[Any, float, str], Any] | None = None,
    frame_adapter: Callable[[Any], bytes] = qimage_to_rgba,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    wall_clock: Callable[[], float] = time.perf_counter,
    cpu_clock: Callable[[], float] = time.process_time,
    encoder_timeout_s: float = ENCODER_TIMEOUT_S,
) -> dict[str, Any]:
    """Render exactly N frames at t=k/fps and atomically publish a new WebM.

    Draw timing is only the renderer callback. Conversion timing is only QImage-to-RGBA.
    Total elapsed includes ffmpeg encoding and publication; parent CPU excludes ffmpeg.
    """
    path, frame_count = validate_export_request(output, fps=fps, duration_s=duration_s)
    if not math.isfinite(encoder_timeout_s) or encoder_timeout_s <= 0:
        raise ValueError("encoder timeout must be finite and positive")

    renderer = None
    if snapshot_factory is None or render_callback is None:
        renderer = _load_cinematic_renderer()
    snapshot = snapshot_factory() if snapshot_factory is not None else _renderer_demo_snapshot(renderer)
    if not bool(getattr(snapshot, "demo", False)):
        raise ExportError("offline export requires snapshot.demo=True so rendered frames prominently say DEMO")
    if render_callback is None:
        render_callback = lambda data, time_s, name: renderer.render_cinematic_image(data, time_s, hostname=name)

    started_wall = wall_clock()
    started_cpu = cpu_clock()
    draw_samples: list[float] = []
    conversion_samples: list[float] = []
    output_fd, temporary_name = tempfile.mkstemp(prefix=".thermalright-cinematic-", suffix=".webm", dir=path.parent)
    temporary_output = Path(temporary_name)
    try:
        with os.fdopen(output_fd, "r+b") as temporary_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
            output_fd = temporary_file.fileno()
            process = popen_factory(
                _ffmpeg_command(output_fd, fps), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr_file,
                pass_fds=(output_fd,), close_fds=True, shell=False,
            )
            try:
                for index in range(frame_count):
                    draw_started = wall_clock()
                    image = render_callback(snapshot, index / fps, hostname)
                    draw_samples.append(_finite_metric(wall_clock() - draw_started, "frame draw"))
                    conversion_started = wall_clock()
                    rgba = frame_adapter(image)
                    conversion_samples.append(_finite_metric(wall_clock() - conversion_started, "frame conversion"))
                    if not isinstance(rgba, (bytes, bytearray, memoryview)) or len(rgba) != FRAME_BYTES:
                        raise ExportError(f"frame {index} is not exactly {FRAME_BYTES} RGBA bytes")
                    if process.stdin.write(rgba) != FRAME_BYTES:
                        raise ExportError(f"ffmpeg accepted only part of frame {index}")
                _close_stdin(process)
                try:
                    returncode = process.wait(timeout=encoder_timeout_s)
                except subprocess.TimeoutExpired as exc:
                    raise ExportError("ffmpeg timed out while encoding") from exc
                if returncode != 0:
                    detail = _stderr_tail(stderr_file)
                    raise ExportError(f"ffmpeg exited with status {returncode}" + (f": {detail}" if detail else ""))
            except Exception as failure:
                try:
                    _stop_encoder(process, encoder_timeout_s)
                except ExportError as cleanup_failure:
                    raise cleanup_failure from failure
                raise

            temporary_file.flush()
            os.fsync(output_fd)
            encoded_bytes = os.fstat(output_fd).st_size
            if encoded_bytes <= 0:
                raise ExportError("ffmpeg reported success but produced an empty WebM")
            elapsed = _finite_metric(wall_clock() - started_wall, "total elapsed")
            parent_cpu = _finite_metric(cpu_clock() - started_cpu, "parent process CPU")
            actual_duration = frame_count / fps
            metrics = {
                "output": str(path),
                "actual_frames": frame_count,
                "actual_duration_seconds": actual_duration,
                "actual_fps": frame_count / actual_duration,
                "render_wall_seconds": _finite_metric(sum(draw_samples), "render wall"),
                "conversion_wall_seconds": _finite_metric(sum(conversion_samples), "conversion wall"),
                "frame_draw_seconds": {"median": _median(draw_samples), "p95": _percentile95(draw_samples)},
                "frame_conversion_seconds": {"median": _median(conversion_samples), "p95": _percentile95(conversion_samples)},
                "total_elapsed_seconds_including_encoding": elapsed,
                "parent_process_cpu_seconds_excluding_ffmpeg": parent_cpu,
                "encoded_file_bytes": encoded_bytes,
                "measurement_notes": ["USB transport was not measured; encoded_file_bytes is the WebM output size."],
            }
            try:
                json.dumps(metrics, allow_nan=False, sort_keys=True)
            except (TypeError, ValueError) as exc:
                raise ExportError("metrics cannot be serialized as finite JSON") from exc
            try:
                os.link(temporary_output, path)
            except FileExistsError as exc:
                raise FileExistsError(f"refusing to overwrite existing output: {path}") from exc
    finally:
        try:
            os.unlink(temporary_output)
        except FileNotFoundError:
            pass

    return metrics


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", required=True, type=Path, metavar="PATH.webm")
    parser.add_argument("--fps", required=True, type=int, choices=ALLOWED_FPS)
    parser.add_argument("--duration", required=True, type=_duration_argument, metavar="SECONDS")
    parser.add_argument("--hostname", help="explicit DEMO title hostname; default is read once from this host")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        hostname = args.hostname if args.hostname is not None else socket.gethostname()
        metrics = export_webm(args.output, fps=args.fps, duration_s=args.duration, hostname=hostname)
    except (ExportError, FileExistsError, FileNotFoundError, ImportError, OSError, ValueError) as exc:
        print(f"cinematic export failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(metrics, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
