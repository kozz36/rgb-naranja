#!/usr/bin/env python3
"""Run the finite cinematic physical-panel trial with its fluid field rendered by GPU."""

from __future__ import annotations

import argparse
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import threading
from typing import Any, Callable


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
JPEG_TIMEOUT_S = 2.0


def _load_sibling(module_name: str, filename: str) -> Any:
    """Load one installed sibling without replacing a module already in this process."""
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_DIRECTORY / filename)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling module {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if sys.modules.get(module_name) is module:
            del sys.modules[module_name]
        raise
    return module


trial = _load_sibling("thermalright_cinematic_trial", "thermalright-cinematic-trial.py")


def _new_sidecar() -> Any:
    """Construct hardware state only from the guarded trial dependency factory."""
    if sys.modules.get("thermalright_cinematic") is not trial.cinematic:
        raise RuntimeError("cinematic CPU module changed during GPU trial setup")
    gpu = _load_sibling("thermalright_cinematic_gpu", "thermalright_cinematic_gpu.py")
    return gpu.GpuFluidSidecar()


def _log_gpu_info(sidecar: Any) -> None:
    info = sidecar.info
    print(
        json.dumps(
            {"type": "cinematic_gpu", "vendor": info.vendor, "renderer": info.renderer, "version": info.version},
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--duration",
        type=trial._duration_argument,
        default=trial.DEFAULT_DURATION_S,
        metavar="SECONDS",
        help="finite physical-trial duration in seconds, greater than 0 and at most 60 (default: 60)",
    )
    parser.add_argument(
        "--jpeg-encoder",
        metavar="ABSOLUTE_PATH",
        help="experimental persistent rust-dashboard-jpeg executable; Qt remains the default",
    )
    return parser


def _validate_encoder_path(value: Path) -> Path:
    """Backwards-compatible trial alias for the stdlib shared-client trust check."""
    jpeg = _load_sibling("thermalright_jpeg", "thermalright_jpeg.py")
    return jpeg.validate_encoder_path(value)


def _new_encoder_session(executable: Path) -> Any:
    jpeg = _load_sibling("thermalright_jpeg", "thermalright_jpeg.py")
    return jpeg.EncoderSession(executable, "420", JPEG_TIMEOUT_S)


def _rgba8888_payload(image: Any) -> bytes:
    """Copy one fixed RGBA8888 frame through the stdlib client without eager Qt loading."""
    from PySide6.QtGui import QImage

    jpeg = _load_sibling("thermalright_jpeg", "thermalright_jpeg.py")
    return jpeg.rgba8888_payload(image, rgba_format=QImage.Format.Format_RGBA8888)


def _rust_jpeg_saver(session: Any) -> Callable[[Any, Path], bool]:
    """Keep the trial's patchable payload seam while reusing the shared image-to-JPEG saver."""
    from PySide6.QtGui import QImage

    jpeg = _load_sibling("thermalright_jpeg", "thermalright_jpeg.py")
    return jpeg.rust_jpeg_saver(
        session,
        rgba_format=QImage.Format.Format_RGBA8888,
        payload_adapter=lambda image: _rgba8888_payload(image),
    )


def _log_encoder_selection(path: Path) -> None:
    print(
        "cinematic GPU trial: experimental Rust JPEG encoder selected "
        f"({path}); parent CPU timing excludes encoder child CPU, with cgroup accounting deferred to an independent runner",
        file=sys.stderr,
        flush=True,
    )


def _cleanup(label: str, resource: Any, errors: list[BaseException]) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except BaseException as exc:
        errors.append(exc)
        print(f"cinematic GPU trial cleanup failed ({label}): {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    """Keep trial guards ahead of deferred GPU/Rust/TRCC initialization."""
    parser = _parser()
    args = parser.parse_args(argv)
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("cinematic GPU trial must run on the main thread")
    encoder_path: Path | None = None
    if args.jpeg_encoder is not None:
        try:
            encoder_path = _validate_encoder_path(Path(args.jpeg_encoder))
        except ValueError as exc:
            parser.error(str(exc))

    original_dependencies = trial._default_dependencies
    sidecar: Any | None = None
    session: Any | None = None
    dependencies: Any | None = None

    def gpu_dependencies() -> Any:
        nonlocal sidecar, session, dependencies
        if dependencies is not None:
            return dependencies
        if sidecar is not None or session is not None:
            raise RuntimeError("GPU trial dependencies did not finish initialization")
        sidecar = _new_sidecar()
        _log_gpu_info(sidecar)
        if encoder_path is not None:
            session = _new_encoder_session(encoder_path)
            _log_encoder_selection(encoder_path)
        field_renderer = sidecar.render

        def render_image(snapshot: Any, elapsed_s: float) -> Any:
            return trial._render_live_image(snapshot, elapsed_s, field_renderer=field_renderer)

        dependencies = replace(
            original_dependencies(),
            render_image=render_image,
            **({"save_jpeg": _rust_jpeg_saver(session)} if session is not None else {}),
        )
        return dependencies

    primary_error: BaseException | None = None
    result = 1
    try:
        # trial.run_trial calls its private-process and lock guards before this factory.
        trial._default_dependencies = gpu_dependencies
        result = trial.main(["--duration", str(args.duration)])
    except BaseException as exc:
        primary_error = exc
    finally:
        trial._default_dependencies = original_dependencies
        cleanup_errors: list[BaseException] = []
        # trial.main has already closed its TRCC app before the owned Rust child and GPU sidecar.
        _cleanup("Rust JPEG encoder", session, cleanup_errors)
        _cleanup("GPU sidecar", sidecar, cleanup_errors)

    if primary_error is not None:
        raise primary_error
    return 1 if cleanup_errors else result


if __name__ == "__main__":
    raise SystemExit(main())
