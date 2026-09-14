#!/usr/bin/env python3
"""Offline comparison of Qt JPEG and the persistent Rust/libturbojpeg prototype.

This program does not build Cargo.  Pass an already-built absolute --encoder path.
Qt, numpy, rendering, and subprocess creation stay out of module import.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import struct
import sys
import tempfile
import time

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
WIDTH, HEIGHT, QUALITY = 1600, 720, 85
WARM_FRAMES = 5


def _load_sibling(module_name, filename):
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


thermalright_jpeg = _load_sibling("thermalright_jpeg", "thermalright_jpeg.py")
EncoderSession = thermalright_jpeg.EncoderSession
ProtocolError = thermalright_jpeg.ProtocolError
MAX_INPUT = thermalright_jpeg.MAX_INPUT
MAX_OUTPUT = thermalright_jpeg.MAX_OUTPUT
STDERR_TAIL_BYTES = thermalright_jpeg.STDERR_TAIL_BYTES
STDERR_READ_CHUNK_BYTES = thermalright_jpeg.STDERR_READ_CHUNK_BYTES
frame_request = thermalright_jpeg.frame_request
parse_response = thermalright_jpeg.parse_response
proc_cpu_seconds = thermalright_jpeg.proc_cpu_seconds


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def benchmark_identity_paths(executable):
    root = Path(__file__).resolve().parents[1]
    return {
        "benchmark": Path(__file__).resolve(),
        "renderer": root / "scripts/thermalright_cinematic.py",
        "gpu_sidecar": root / "scripts/thermalright_cinematic_gpu.py",
        "jpeg_client": root / "scripts/thermalright_jpeg.py",
        "rust_source": root / "experiments/rust-dashboard-bench/src/main.rs",
        "encoder_executable": Path(executable),
    }


def capture_hashes(paths, hash_file=sha256_file):
    """Capture a complete named identity set without inventing a successful result."""
    hashes, errors = {}, {}
    for name, path in paths.items():
        path = Path(path)
        try:
            if not path.is_file():
                raise OSError("path is not a regular file")
            hashes[name] = hash_file(path)
        except Exception as error:
            errors[name] = str(error)
    return {"complete": not errors, "hashes": hashes, "errors": errors}


def compare_hash_captures(start, end):
    """Fail closed when either capture is incomplete or a named input changed."""
    start_hashes, end_hashes = start["hashes"], end["hashes"]
    all_names = sorted(set(start_hashes) | set(end_hashes) | set(start["errors"]) | set(end["errors"]))
    drift_paths = [name for name in all_names if start_hashes.get(name) != end_hashes.get(name)]
    complete = start["complete"] and end["complete"] and not drift_paths
    return {
        "status": "valid" if complete else "invalid",
        "capture_complete": start["complete"] and end["complete"],
        "start": start_hashes,
        "end": end_hashes,
        "drift": not complete,
        "drift_paths": drift_paths,
        "capture_errors": {"start": start["errors"], "end": end["errors"]},
    }


def bounded_int(name, value, minimum, maximum):
    value = int(value)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def percentile(values, percent):
    return sorted(values)[max(0, math.ceil(len(values) * percent / 100) - 1)]


def _qt_dependencies():
    import numpy as np
    from PySide6 import QtCore, QtGui
    return np, QtCore, QtGui


def _tight_rgba(image):
    image = image.convertToFormat(image.Format.Format_RGBA8888)
    if (image.width(), image.height()) != (WIDTH, HEIGHT):
        raise RuntimeError("renderer returned non-fixed dimensions")
    raw, stride = bytes(image.constBits()), image.bytesPerLine()
    return b"".join(raw[row * stride:row * stride + WIDTH * 4] for row in range(HEIGHT))


def _render_cases(cinematic, field_renderer=None):
    cases = [("t0", 0.0, 58.0, 63.0), ("t4_54", 4.54, 58.0, 63.0),
             ("t10", 10.0, 58.0, 63.0), ("t30", 30.0, 58.0, 63.0),
             ("t60", 60.0, 58.0, 63.0), ("t150", 150.0, 58.0, 63.0),
             ("t525", 525.0, 58.0, 63.0), ("t900", 900.0, 58.0, 63.0),
             ("na", 10.0, None, None), ("zero", 30.0, 0.0, 0.0),
             ("high", 60.0, 100.0, 100.0), ("mixed_na", 150.0, None, 100.0)]
    corpus = []
    for name, phase, cpu_temp, gpu_temp in cases:
        snapshot = cinematic._synthetic_snapshot()
        snapshot.cpu.temp, snapshot.gpu.temp = cpu_temp, gpu_temp
        image = cinematic.render_cinematic_image(snapshot, phase, hostname="MASTER", field_renderer=field_renderer)
        corpus.append({"case": name, "time_s": phase, "rgba": _tight_rgba(image), "image": image.copy()})
    return corpus


def build_corpus(gpu_corpus):
    import sys
    scripts = str(Path(__file__).resolve().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import thermalright_cinematic as cinematic
    if not gpu_corpus:
        return _render_cases(cinematic), "cpu-renderer (pixel-identical CPU renderer; not GPU output)"
    from thermalright_cinematic_gpu import GpuFluidSidecar
    with GpuFluidSidecar() as sidecar:
        corpus = _render_cases(cinematic, sidecar.render)
    return corpus, "offline GPU field sidecar; context closed before codec measurement"


def jpeg_sof_info(jpeg):
    if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
        raise ProtocolError("not a complete JPEG")
    index = 2
    while index + 4 <= len(jpeg):
        if jpeg[index] != 0xff:
            index += 1
            continue
        while index < len(jpeg) and jpeg[index] == 0xff:
            index += 1
        marker = jpeg[index]; index += 1
        if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
            continue
        if index + 2 > len(jpeg): break
        length, = struct.unpack(">H", jpeg[index:index + 2])
        segment = jpeg[index + 2:index + length]
        if length < 2 or index + length > len(jpeg): break
        if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
            height, width, count = struct.unpack(">HHB", segment[1:6])
            components = [(segment[6 + 3 * n + 1] >> 4, segment[6 + 3 * n + 1] & 15) for n in range(count)]
            sampling = "444" if all(pair == (1, 1) for pair in components) else "420" if components and components[0] == (2, 2) and all(pair == (1, 1) for pair in components[1:]) else "other"
            return {"width": width, "height": height, "subsampling": sampling}
        index += length
    raise ProtocolError("JPEG has no readable SOF segment")


def _psnr_metrics(mae, mse):
    if not all(isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 for value in (mae, mse)):
        raise ValueError("image quality metrics must be finite and non-negative")
    perfect = mse == 0.0
    return {"mae": mae, "mse": mse, "psnr_db": None if perfect else 10 * math.log10(255 * 255 / mse),
            "perfect": perfect}


def _quality(corpus, encoded):
    np, QtCore, QtGui = _qt_dependencies()
    maes, mses, cases = [], [], []
    for frame in corpus:
        case = {"case_id": frame["case"], "time_s": frame["time_s"], "status": "passed"}
        try:
            jpeg = encoded[frame["case"]]
            info = jpeg_sof_info(jpeg)
            case["jpeg"] = {"bytes": len(jpeg), "subsampling": info["subsampling"],
                            "resolution": {"width": info["width"], "height": info["height"]}}
            if (info["width"], info["height"]) != (WIDTH, HEIGHT):
                raise ProtocolError("JPEG dimensions do not match the fixed corpus")
            decoded = QtGui.QImage.fromData(QtCore.QByteArray(jpeg))
            if decoded.isNull():
                raise ProtocolError("Qt JPEG decode failed")
            decoded = decoded.convertToFormat(QtGui.QImage.Format.Format_RGB888)
            if (decoded.width(), decoded.height()) != (WIDTH, HEIGHT):
                raise ProtocolError("Qt decoder dimensions do not match the fixed corpus")
            raw, stride = bytes(decoded.constBits()), decoded.bytesPerLine()
            rgb = np.frombuffer(b"".join(raw[y * stride:y * stride + WIDTH * 3] for y in range(HEIGHT)), dtype=np.uint8).reshape(HEIGHT, WIDTH, 3)
            reference = np.frombuffer(frame["rgba"], dtype=np.uint8).reshape(HEIGHT, WIDTH, 4)[..., :3]
            delta = rgb.astype(np.float32) - reference.astype(np.float32)
            mae, mse = float(np.abs(delta).mean()), float((delta * delta).mean())
            case["reference_vs_encoded"] = _psnr_metrics(mae, mse)
            maes.append(mae); mses.append(mse)
        except (KeyError, OSError, ProtocolError, RuntimeError, ValueError, struct.error) as error:
            case["status"] = "failed"
            case["error"] = str(error)
            case.setdefault("jpeg", {"bytes": 0, "subsampling": None, "resolution": None})
            case["reference_vs_encoded"] = {"mae": None, "mse": None, "psnr_db": None, "perfect": False}
        cases.append(case)
    failed_case_ids = [case["case_id"] for case in cases if case["status"] != "passed"]
    mse = statistics.fmean(mses) if mses else None
    summary_metrics = _psnr_metrics(statistics.fmean(maes), mse) if mse is not None else {
        "mae": None, "mse": None, "psnr_db": None, "perfect": False}
    return {"status": "passed" if not failed_case_ids else "failed", "cases": cases,
            "summary": {"case_count": len(cases), "passed_case_count": len(cases) - len(failed_case_ids),
                        "failed_case_ids": failed_case_ids, "reference_vs_encoded": summary_metrics}}


def _summary(name, samples, outputs, parent_cpu, child_cpu, encoded, corpus):
    wall_samples_ms = [sample * 1e3 for sample in samples]
    return {"path": name, "frames": len(samples), "wall_median_ms": statistics.median(wall_samples_ms),
            "wall_p95_ms": percentile(wall_samples_ms, 95), "wall_average_ms": statistics.fmean(wall_samples_ms),
            "wall_samples_ms": wall_samples_ms, "wall_under_24hz_feasible": statistics.fmean(samples) < 1 / 24,
            "cpu_parent_s": parent_cpu, "cpu_child_s": child_cpu, "cpu_total_s": parent_cpu + child_cpu,
            "jpeg_bytes_mean": statistics.fmean(outputs), "jpeg_bytes_p95": percentile(outputs, 95),
            "jpeg_bytes_samples": outputs, "quality": _quality(corpus, encoded)}


def _measure(name, corpus, frames, encode, child=None, warm_frames=WARM_FRAMES, collect=lambda value: value):
    for index in range(warm_frames):
        collect(encode(corpus[index % len(corpus)]))
    child_start = child.cpu_seconds() if child else 0.0
    samples, outputs, encoded, parent_cpu = [], [], {}, 0.0
    for index in range(frames):
        frame = corpus[index % len(corpus)]; cpu_start = time.process_time(); start = time.perf_counter(); value = encode(frame); samples.append(time.perf_counter() - start)
        parent_cpu += time.process_time() - cpu_start
        jpeg = collect(value); outputs.append(len(jpeg)); encoded.setdefault(frame["case"], jpeg)
    child_end = child.cpu_seconds() if child else 0.0
    return _summary(name, samples, outputs, parent_cpu, max(0.0, child_end - child_start), encoded, corpus)


def _qt_memory(frame):
    _, QtCore, QtGui = _qt_dependencies()
    buffer = QtCore.QBuffer(); buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    if not frame["image"].save(buffer, "JPEG", QUALITY):
        raise RuntimeError("Qt QBuffer JPEG encoding failed")
    return bytes(buffer.data())


def _qt_file_encoder(directory):
    def encode(frame):
        path = directory / "qt.jpg"
        if not frame["image"].save(str(path), "JPEG", QUALITY):
            raise RuntimeError("Qt tempfile JPEG encoding failed")
        return path
    return encode


def _rust_encoder(executable, subsampling, timeout_s, startup, first_rgba):
    start = time.perf_counter(); session = EncoderSession(executable, subsampling, timeout_s)
    startup["spawn_wall_ms"] = (time.perf_counter() - start) * 1e3
    try:
        first = time.perf_counter(); session.request(first_rgba)
        startup["first_warm_request_wall_ms"] = (time.perf_counter() - first) * 1e3
        return session
    except BaseException:
        session.abort()
        raise


def _rust_file_encoder(session, directory):
    def encode(frame):
        jpeg = session.request(frame["rgba"])
        with (directory / "rust.jpg").open("wb", buffering=0) as output:
            view = memoryview(jpeg)
            while view:
                count = output.write(view)
                if not count: raise OSError("short tempfile write")
                view = view[count:]
        return jpeg
    return encode


def _aggregate(entries):
    pooled_wall = [sample for entry in entries for sample in entry["wall_samples_ms"]]
    pooled_bytes = [sample for entry in entries for sample in entry.get("jpeg_bytes_samples", [entry["jpeg_bytes_mean"]])]
    if not pooled_wall:
        raise ValueError("cannot aggregate an empty frame sample set")
    round_details = []
    for index, entry in enumerate(entries, start=1):
        round_details.append({"round": index, "wall_median_ms": entry["wall_median_ms"],
                              "wall_p95_ms": entry["wall_p95_ms"], "wall_average_ms": entry["wall_average_ms"],
                              "cpu_parent_s": entry["cpu_parent_s"], "cpu_child_s": entry["cpu_child_s"],
                              "cpu_total_s": entry["cpu_total_s"]})
    quality = dict(entries[-1]["quality"])
    round_quality_status = [entry["quality"]["status"] for entry in entries]
    if any(status != "passed" for status in round_quality_status):
        quality["status"] = "failed"
    quality["round_statuses"] = round_quality_status
    quality["failed_round_case_ids"] = sorted({case_id for entry in entries
                                                for case_id in entry["quality"].get("summary", {}).get("failed_case_ids", [])})
    return {
        "rounds": len(entries), "wall_median_ms": statistics.median(pooled_wall),
        "wall_p95_ms": percentile(pooled_wall, 95), "wall_average_ms": statistics.fmean(pooled_wall),
        "wall_samples_ms": pooled_wall, "wall_under_24hz_feasible": statistics.fmean(pooled_wall) < 1000 / 24,
        "cpu_parent_mean_s": statistics.fmean(entry["cpu_parent_s"] for entry in entries),
        "cpu_parent_median_s": statistics.median(entry["cpu_parent_s"] for entry in entries),
        "cpu_child_mean_s": statistics.fmean(entry["cpu_child_s"] for entry in entries),
        "cpu_child_median_s": statistics.median(entry["cpu_child_s"] for entry in entries),
        "cpu_total_mean_s": statistics.fmean(entry["cpu_total_s"] for entry in entries),
        "cpu_total_median_s": statistics.median(entry["cpu_total_s"] for entry in entries),
        "cpu_total_sum_s": sum(entry["cpu_total_s"] for entry in entries),
        "jpeg_bytes_mean": statistics.fmean(pooled_bytes), "jpeg_bytes_p95": percentile(pooled_bytes, 95),
        "jpeg_bytes_samples": pooled_bytes, "quality": quality, "per_round": round_details,
    }


def run_benchmark(executable, corpus, rounds, frames, subsampling, timeout_s):
    results = {name: [] for name in ("qt_memory", "rust_memory", "qt_file", "rust_file")}
    startup = []
    with tempfile.TemporaryDirectory(prefix="rust-dashboard-jpeg-") as temporary:
        directory = Path(temporary)
        for round_index in range(rounds):
            order = ["qt_memory", "rust_memory", "qt_file", "rust_file"]
            if round_index % 2:
                order.reverse()
            for name in order:
                if name == "qt_memory":
                    result = _measure(name, corpus, frames, _qt_memory)
                elif name == "qt_file":
                    result = _measure(name, corpus, frames, _qt_file_encoder(directory), collect=lambda path: path.read_bytes())
                else:
                    record = {}
                    session = _rust_encoder(executable, subsampling, timeout_s, record, corpus[0]["rgba"])
                    try:
                        encode = session.request if name == "rust_memory" else _rust_file_encoder(session, directory)
                        result = _measure(name, corpus, frames,
                                          lambda frame: encode(frame["rgba"]) if name == "rust_memory" else encode(frame),
                                          session, WARM_FRAMES - 1)
                    except BaseException:
                        session.abort()
                        raise
                    else:
                        session.close()
                    finally:
                        record["stderr"] = session.stderr_report()
                        startup.append(record)
                results[name].append(result)
    return {name: _aggregate(entries) for name, entries in results.items()}, startup


def _quality_case_map(quality):
    cases, duplicates = {}, []
    for case in quality.get("cases", []):
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or case_id in cases:
            duplicates.append(str(case_id))
        else:
            cases[case_id] = case
    return cases, duplicates


def _quality_comparison(qt_quality, rust_quality):
    qt_cases, qt_duplicates = _quality_case_map(qt_quality)
    rust_cases, rust_duplicates = _quality_case_map(rust_quality)
    case_ids = list(qt_cases) + [case_id for case_id in rust_cases if case_id not in qt_cases]
    failed_case_ids, case_reports, finite_deltas, size_growths = [], [], [], []
    unbounded_delta = False
    for case_id in case_ids:
        qt_case, rust_case = qt_cases.get(case_id), rust_cases.get(case_id)
        report = {"case_id": case_id, "status": "passed", "psnr_delta_db": None,
                  "psnr_delta_unbounded": False, "size_growth_percent": None}
        if qt_case is not None:
            report["time_s"] = qt_case.get("time_s")
            report["reference_vs_qt"] = qt_case.get("reference_vs_encoded")
            report["qt_jpeg"] = qt_case.get("jpeg")
        if rust_case is not None:
            report.setdefault("time_s", rust_case.get("time_s"))
            report["reference_vs_rust"] = rust_case.get("reference_vs_encoded")
            report["rust_jpeg"] = rust_case.get("jpeg")
        try:
            if qt_case is None or rust_case is None:
                raise ValueError("case ID is not present in both paths")
            if qt_case.get("status") != "passed" or rust_case.get("status") != "passed":
                raise ValueError("JPEG decode or corpus verification failed")
            if qt_case.get("time_s") != rust_case.get("time_s"):
                raise ValueError("case time does not match")
            qt_metrics, rust_metrics = qt_case["reference_vs_encoded"], rust_case["reference_vs_encoded"]
            qt_jpeg, rust_jpeg = qt_case["jpeg"], rust_case["jpeg"]
            if qt_jpeg["resolution"] != rust_jpeg["resolution"] or qt_jpeg["resolution"] != {"width": WIDTH, "height": HEIGHT}:
                raise ValueError("JPEG resolution does not match across paths")
            qt_bytes, rust_bytes = qt_jpeg["bytes"], rust_jpeg["bytes"]
            if not isinstance(qt_bytes, int) or not isinstance(rust_bytes, int) or qt_bytes <= 0:
                raise ValueError("invalid JPEG byte count")
            growth = (rust_bytes / qt_bytes - 1) * 100
            report["size_growth_percent"] = growth
            size_growths.append(growth)
            qt_perfect, rust_perfect = bool(qt_metrics["perfect"]), bool(rust_metrics["perfect"])
            if qt_perfect and rust_perfect:
                delta = 0.0
            elif qt_perfect:
                report["psnr_delta_unbounded"] = True
                unbounded_delta = True
                raise ValueError("Qt reference is perfect but Rust is not")
            elif rust_perfect:
                delta = None
                report["psnr_delta_perfect_improvement"] = True
            else:
                qt_psnr, rust_psnr = qt_metrics["psnr_db"], rust_metrics["psnr_db"]
                if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (qt_psnr, rust_psnr)):
                    raise ValueError("PSNR must be finite unless the image is perfect")
                delta = qt_psnr - rust_psnr
            report["psnr_delta_db"] = delta
            if delta is not None:
                finite_deltas.append(delta)
                if delta > 0.2:
                    raise ValueError("PSNR degradation exceeds 0.2 dB")
            if growth > 10.0:
                raise ValueError("JPEG size growth exceeds 10 percent")
        except (KeyError, TypeError, ValueError) as error:
            report["status"] = "failed"
            report["error"] = str(error)
            failed_case_ids.append(case_id)
        case_reports.append(report)
    failed_case_ids.extend(qt_duplicates + rust_duplicates)
    failed_case_ids.extend(qt_quality.get("failed_round_case_ids", []) + rust_quality.get("failed_round_case_ids", []))
    failed_case_ids = list(dict.fromkeys(failed_case_ids))
    quality_ok = not failed_case_ids and qt_quality.get("status") == "passed" and rust_quality.get("status") == "passed"
    worst_delta = None if unbounded_delta else (max(finite_deltas) if finite_deltas else None)
    return {"status": "passed" if quality_ok else "failed", "case_count": len(case_reports),
            "aligned_case_ids": not qt_duplicates and not rust_duplicates and set(qt_cases) == set(rust_cases),
            "failed_case_ids": failed_case_ids, "worst_psnr_delta_db": worst_delta,
            "worst_psnr_delta_unbounded": unbounded_delta,
            "worst_size_growth_percent": max(size_growths) if size_growths else None,
            "thresholds": {"psnr_degradation_db": 0.2, "jpeg_size_growth_percent": 10.0},
            "qt_summary": qt_quality.get("summary"), "rust_summary": rust_quality.get("summary"), "cases": case_reports}


def _paired_cpu_gain(qt, rust):
    qt_rounds, rust_rounds = qt.get("per_round", []), rust.get("per_round", [])
    if len(qt_rounds) != len(rust_rounds) or not qt_rounds:
        return None
    gains = []
    for qt_round, rust_round in zip(qt_rounds, rust_rounds):
        qt_cpu, rust_cpu = qt_round.get("cpu_total_s"), rust_round.get("cpu_total_s")
        if not isinstance(qt_cpu, (int, float)) or not isinstance(rust_cpu, (int, float)) or qt_cpu <= 0:
            return None
        gains.append(1 - rust_cpu / qt_cpu)
    return statistics.median(gains)


def _comparison(qt, rust, identity_valid=True):
    quality = _quality_comparison(qt["quality"], rust["quality"])
    cpu_gain = _paired_cpu_gain(qt, rust)
    return {"identity_valid": identity_valid, "cpu_gain_median_paired": cpu_gain,
            "cpu_target_20_percent": identity_valid and cpu_gain is not None and cpu_gain >= .20,
            "p95_not_worse": identity_valid and rust["wall_p95_ms"] <= qt["wall_p95_ms"],
            "quality_gate": identity_valid and quality["status"] == "passed", "quality": quality}


def build_parser():
    parser = argparse.ArgumentParser(description="Offline matched Qt vs persistent Rust JPEG benchmark; does not build Cargo.")
    parser.add_argument("--encoder", required=True, help="absolute path to an already-built rust-dashboard-jpeg executable")
    parser.add_argument("--output", help="new JSON output path; never overwritten")
    parser.add_argument("--rounds", default=3, type=int); parser.add_argument("--frames", default=120, type=int)
    parser.add_argument("--timeout-s", default=10.0, type=float); parser.add_argument("--gpu-corpus", action="store_true")
    parser.add_argument("--rust-subsampling", choices=("420", "444"), default="420")
    return parser


def main(argv=None):
    parser = build_parser(); args = parser.parse_args(argv)
    try:
        rounds, frames = bounded_int("rounds", args.rounds, 1, 5), bounded_int("frames", args.frames, 12, 288)
        if not args.output: raise ValueError("--output is required")
        executable, output = Path(args.encoder), Path(args.output)
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK): raise ValueError("--encoder must be an executable absolute path")
        if output.exists() or not output.parent.is_dir(): raise ValueError("--output must be a new file under an existing directory")
        if not math.isfinite(args.timeout_s) or args.timeout_s <= 0: raise ValueError("--timeout-s must be finite and positive")
    except ValueError as error:
        parser.error(str(error))
    identity_paths = benchmark_identity_paths(executable)
    start_capture = capture_hashes(identity_paths)
    if not start_capture["complete"]:
        parser.error("benchmark identity capture failed before corpus build")
    # The end capture is in finally so a measurement error cannot be mistaken for an unchanged input set.
    try:
        corpus, corpus_note = build_corpus(args.gpu_corpus)
        results, startup = run_benchmark(executable, corpus, rounds, frames, args.rust_subsampling, args.timeout_s)
    finally:
        end_capture = capture_hashes(identity_paths)
    identity = compare_hash_captures(start_capture, end_capture)
    identity_valid = identity["status"] == "valid"
    gates = {
        "memory": _comparison(results["qt_memory"], results["rust_memory"], identity_valid),
        "file": _comparison(results["qt_file"], results["rust_file"], identity_valid),
        "note": "20% CPU / p95 / per-case quality thresholds are initial experimental gates, not production claims.",
    }
    quality_valid = all(gates[name]["quality"]["status"] == "passed" for name in ("memory", "file"))
    status = "invalid" if not identity_valid else "completed" if quality_valid else "failed"
    report = {
        "status": status,
        "prototype": "Rust std + explicit C FFI to system libturbojpeg; not a Rust-language causality claim",
        "config": {"quality": QUALITY, "rust_requested_subsampling": args.rust_subsampling, "rounds": rounds,
                   "frames_per_round": frames, "warm_frames_excluded": WARM_FRAMES, "corpus": corpus_note},
        "payload": {"rgba_bytes_per_frame": MAX_INPUT, "decimal_mb_per_s_at_24hz": MAX_INPUT * 24 / 1e6,
                    "note": "payload volume only; no OS copy count is inferred"},
        "cpu_accounting": {"primary": "parent process_time plus child /proc/<pid>/stat utime+stime deltas",
                           "child_tick_s": 1 / os.sysconf("SC_CLK_TCK"),
                           "note": "child ticks are quantized; CPU comparison uses median paired round totals."},
        "source_sha256": identity, "results": results, "rust_startup": startup, "gates": gates,
    }
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(f"wrote {output}")
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
