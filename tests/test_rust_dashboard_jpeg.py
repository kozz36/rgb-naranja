"""Focused contracts for the isolated Rust/libturbojpeg prototype."""
import importlib.util
import struct
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_rust_dashboard_jpeg.py"
SHARED_CLIENT = ROOT / "scripts" / "thermalright_jpeg.py"


def load_shared_client():
    existing = sys.modules.get("thermalright_jpeg")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("thermalright_jpeg", SHARED_CLIENT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if sys.modules.get(spec.name) is module:
            del sys.modules[spec.name]
        raise
    return module


def load_benchmark():
    spec = importlib.util.spec_from_file_location("rust_dashboard_jpeg_bench", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ImportContracts(unittest.TestCase):
    def test_import_does_not_load_qt_or_start_a_process(self):
        sys.modules.pop("rust_dashboard_jpeg_bench", None)
        before = {name for name in sys.modules if name.startswith(("PySide6", "numpy"))}
        bench = load_benchmark()
        after = {name for name in sys.modules if name.startswith(("PySide6", "numpy"))}
        self.assertEqual(after, before)
        self.assertTrue(callable(bench.main))

    def test_shared_client_is_import_pure_and_benchmark_reexports_its_identity(self):
        sys.modules.pop("thermalright_jpeg", None)
        sys.modules.pop("rust_dashboard_jpeg_bench", None)
        before = {name for name in sys.modules if name.startswith(("PySide6", "numpy"))}
        with patch("subprocess.Popen", side_effect=AssertionError("import must not launch a child")) as popen:
            shared = load_shared_client()
            bench = load_benchmark()
        after = {name for name in sys.modules if name.startswith(("PySide6", "numpy"))}
        self.assertEqual(after, before)
        popen.assert_not_called()
        self.assertIs(sys.modules["thermalright_jpeg"], shared)
        self.assertIs(bench.EncoderSession, shared.EncoderSession)
        self.assertIs(bench.ProtocolError, shared.ProtocolError)


class _FakeChild:
    """Deterministic protocol stand-in: unit tests never require Cargo output."""
    def __init__(self):
        self.requests = 0

    def reply(self, wire):
        length, = struct.unpack(">I", wire[:4])
        assert len(wire) == length + 4
        self.requests += 1
        return struct.pack(">I", 4) + b"\xff\xd8\xff\xd9"


class ProtocolContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bench = load_benchmark()

    def test_request_and_response_framing_have_bounded_lengths(self):
        with self.assertRaisesRegex(self.bench.ProtocolError, "invalid RGBA"):
            self.bench.frame_request(b"abc")
        request = self.bench.frame_request(bytes(self.bench.MAX_INPUT))
        self.assertEqual(request[:4], struct.pack(">I", self.bench.MAX_INPUT))
        self.assertEqual(self.bench.parse_response(struct.pack(">I", 2) + b"\xff\xd8"), b"\xff\xd8")
        with self.assertRaisesRegex(self.bench.ProtocolError, "truncated JPEG response"):
            self.bench.parse_response(struct.pack(">I", 3) + b"\xff\xd8")
        with self.assertRaisesRegex(self.bench.ProtocolError, "output limit"):
            self.bench.parse_response(struct.pack(">I", self.bench.MAX_OUTPUT + 1))

    def test_fake_child_protocol_is_deterministic_without_an_encoder_binary(self):
        child = _FakeChild()
        reply = child.reply(self.bench.frame_request(bytes(self.bench.MAX_INPUT)))
        self.assertEqual((child.requests, self.bench.parse_response(reply)), (1, b"\xff\xd8\xff\xd9"))

    def test_process_stat_cpu_parser_uses_utime_and_stime_after_comm(self):
        stat = "123 (worker) R " + " ".join(str(i) for i in range(1, 20))
        self.assertAlmostEqual(self.bench.proc_cpu_seconds(stat, 10), (11 + 12) / 10)
        with self.assertRaisesRegex(ValueError, "stat"):
            self.bench.proc_cpu_seconds("broken", 100)

    def test_p95_and_cli_limits_are_explicit(self):
        self.assertEqual(self.bench.percentile([1.0, 2.0, 3.0, 4.0], 95), 4.0)
        self.assertEqual(self.bench.bounded_int("rounds", 1, 1, 5), 1)
        with self.assertRaisesRegex(ValueError, "rounds"):
            self.bench.bounded_int("rounds", 6, 1, 5)
        parser = self.bench.build_parser()
        args = parser.parse_args(["--encoder", "/tmp/encoder", "--rounds", "5", "--frames", "288"])
        self.assertEqual((args.rounds, args.frames), (5, 288))


class JpegAdapterContracts(unittest.TestCase):
    def test_fixed_rgba_adapter_and_jpeg_writer_are_bounded_and_exact(self):
        shared = load_shared_client()
        rgba_format = object()
        payload = b"R" * shared.MAX_INPUT

        class Image:
            def __init__(self, stride: int = 6400) -> None:
                self.stride = stride
                self.converted_to: object | None = None

            def isNull(self) -> bool:
                return False

            def convertToFormat(self, value: object) -> "Image":
                self.converted_to = value
                return self

            def format(self) -> object:
                return rgba_format

            def width(self) -> int:
                return 1600

            def height(self) -> int:
                return 720

            def bytesPerLine(self) -> int:
                return self.stride

            def constBits(self) -> bytes:
                return payload

        image = Image()
        self.assertEqual(shared.rgba8888_payload(image, rgba_format=rgba_format), payload)
        self.assertIs(image.converted_to, rgba_format)
        with self.assertRaisesRegex(ValueError, "RGBA8888"):
            shared.rgba8888_payload(Image(stride=4), rgba_format=rgba_format)

        requests: list[bytes] = []

        class Session:
            def request(self, frame: bytes) -> bytes:
                requests.append(frame)
                return b"\xff\xd8rust\xff\xd9"

        saver = shared.rust_jpeg_saver(Session(), rgba_format=rgba_format)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "frame.jpeg"
            self.assertTrue(saver(image, output))
            self.assertEqual(output.read_bytes(), b"\xff\xd8rust\xff\xd9")
        self.assertEqual(requests, [payload])


class HarnessRegressionContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bench = load_benchmark()

    def _quality(self, cases, status="passed"):
        return {"status": status, "cases": cases, "summary": {"case_count": len(cases)}}

    def _case(self, case_id, psnr_db, jpeg_bytes, *, perfect=False, status="passed", width=1600, height=720):
        return {"case_id": case_id, "time_s": 0.0, "status": status,
                "reference_vs_encoded": {"mae": 1.0, "mse": 1.0, "psnr_db": psnr_db, "perfect": perfect},
                "jpeg": {"bytes": jpeg_bytes, "subsampling": "420",
                         "resolution": {"width": width, "height": height}}}

    def _path_result(self, quality, samples):
        return {"quality": quality, "wall_p95_ms": 10.0, "wall_median_ms": 5.0,
                "wall_average_ms": 5.0, "cpu_total_median_s": 2.0,
                "per_round": [{"cpu_total_s": 2.0}], "wall_samples_ms": samples}

    def test_stderr_flood_does_not_block_response_and_failed_children_are_reaped(self):
        response = (
            "import struct,sys; sys.stderr.buffer.write(b'x' * (1024 * 1024)); sys.stderr.flush(); "
            "size=struct.unpack('>I', sys.stdin.buffer.read(4))[0]; sys.stdin.buffer.read(size); "
            "sys.stdout.buffer.write(struct.pack('>I', 4) + b'\\xff\\xd8\\xff\\xd9'); sys.stdout.buffer.flush()"
        )
        session = self.bench.EncoderSession(Path(sys.executable), "420", 1.0,
                                            argv=[sys.executable, "-u", "-c", response])
        started = time.monotonic()
        self.assertEqual(session.request(b"payload"), b"\xff\xd8\xff\xd9")
        self.assertLess(time.monotonic() - started, 2.0)
        session.close()
        report = session.stderr_report()
        self.assertTrue(report["truncated"])
        self.assertGreater(report["total_bytes"], self.bench.STDERR_TAIL_BYTES)
        self.assertLessEqual(report["tail_bytes"], self.bench.STDERR_TAIL_BYTES)

        for code in (
            "import sys; sys.stderr.buffer.write(b'e' * (1024 * 1024)); sys.stderr.flush(); sys.exit(9)",
            "import sys,time; sys.stderr.buffer.write(b'h' * (1024 * 1024)); sys.stderr.flush(); time.sleep(60)",
        ):
            session = self.bench.EncoderSession(Path(sys.executable), "420", 0.25,
                                                argv=[sys.executable, "-u", "-c", code])
            pid, started = session.process.pid, time.monotonic()
            with self.assertRaises(self.bench.ProtocolError):
                session.request(b"payload")
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertIsNotNone(session.process.poll())
            self.assertFalse(Path(f"/proc/{pid}").exists())
            report = session.stderr_report()
            self.assertTrue(report["truncated"])
            self.assertLessEqual(report["tail_bytes"], self.bench.STDERR_TAIL_BYTES)

    def test_quality_gate_aligns_cases_and_rejects_worst_case_or_decode_failure(self):
        perfect_metrics = self.bench._psnr_metrics(0.0, 0.0)
        self.assertEqual(perfect_metrics, {"mae": 0.0, "mse": 0.0, "psnr_db": None, "perfect": True})
        qt = self._quality([self._case("good", 40.0, 100), self._case("bad", 50.0, 100)])
        rust = self._quality([self._case("good", 40.0, 100), self._case("bad", 49.7, 111)])
        comparison = self.bench._comparison(self._path_result(qt, [1.0]), self._path_result(rust, [1.0]))
        self.assertFalse(comparison["quality_gate"])
        self.assertEqual(comparison["quality"]["failed_case_ids"], ["bad"])
        self.assertAlmostEqual(comparison["quality"]["worst_psnr_delta_db"], 0.3)
        self.assertAlmostEqual(comparison["quality"]["worst_size_growth_percent"], 11.0)
        self.assertIn("reference_vs_qt", comparison["quality"]["cases"][0])
        self.assertIn("reference_vs_rust", comparison["quality"]["cases"][0])

        perfect_qt = self._quality([self._case("perfect", None, 100, perfect=True)])
        degraded_rust = self._quality([self._case("perfect", 35.0, 100)])
        perfect_comparison = self.bench._comparison(
            self._path_result(perfect_qt, [1.0]), self._path_result(degraded_rust, [1.0]))
        self.assertFalse(perfect_comparison["quality_gate"])
        self.assertTrue(perfect_comparison["quality"]["worst_psnr_delta_unbounded"])
        both_perfect = self.bench._comparison(
            self._path_result(perfect_qt, [1.0]), self._path_result(perfect_qt, [1.0]))
        self.assertTrue(both_perfect["quality_gate"])
        self.assertEqual(both_perfect["quality"]["worst_psnr_delta_db"], 0.0)

        decode_failure = self._quality([self._case("good", 40.0, 100),
                                        self._case("bad", None, 0, status="failed", width=0, height=0)], "failed")
        failed = self.bench._comparison(self._path_result(qt, [1.0]), self._path_result(decode_failure, [1.0]))
        self.assertFalse(failed["quality_gate"])
        self.assertEqual(failed["quality"]["failed_case_ids"], ["bad"])
        dimension_mismatch = self._quality([self._case("good", 40.0, 100), self._case("bad", 50.0, 100, height=719)])
        mismatched = self.bench._comparison(self._path_result(qt, [1.0]), self._path_result(dimension_mismatch, [1.0]))
        self.assertFalse(mismatched["quality_gate"])
        self.assertEqual(mismatched["quality"]["failed_case_ids"], ["bad"])
        missing_case = self.bench._comparison(self._path_result(qt, [1.0]),
                                              self._path_result(self._quality([self._case("good", 40.0, 100)]), [1.0]))
        self.assertFalse(missing_case["quality_gate"])
        self.assertEqual(missing_case["quality"]["failed_case_ids"], ["bad"])

    def test_identity_capture_fails_closed_on_drift_or_hash_error(self):
        identity_paths = self.bench.benchmark_identity_paths(Path(sys.executable))
        self.assertEqual(identity_paths["jpeg_client"], SHARED_CLIENT)
        calls = []
        def changing_hash(_path):
            calls.append(1)
            return "start" if len(calls) == 1 else "end"

        start = self.bench.capture_hashes({"benchmark": SCRIPT}, changing_hash)
        end = self.bench.capture_hashes({"benchmark": SCRIPT}, changing_hash)
        identity = self.bench.compare_hash_captures(start, end)
        self.assertEqual(identity["status"], "invalid")
        self.assertTrue(identity["drift"])
        self.assertEqual(identity["drift_paths"], ["benchmark"])
        self.assertNotIn("files_unchanged", identity)
        quality = self._quality([self._case("only", 40.0, 100)])
        invalid_gate = self.bench._comparison(self._path_result(quality, [1.0]), self._path_result(quality, [1.0]), False)
        self.assertFalse(invalid_gate["cpu_target_20_percent"])
        self.assertFalse(invalid_gate["p95_not_worse"])
        self.assertFalse(invalid_gate["quality_gate"])

        failed = self.bench.capture_hashes({"benchmark": SCRIPT}, lambda _path: (_ for _ in ()).throw(OSError("no hash")))
        invalid = self.bench.compare_hash_captures(failed, end)
        self.assertEqual(invalid["status"], "invalid")
        self.assertFalse(invalid["capture_complete"])

    def test_aggregate_p95_is_pooled_not_mean_of_round_p95(self):
        quality = self._quality([])
        def entry(samples):
            return {"wall_median_ms": 1.0, "wall_p95_ms": max(samples), "wall_average_ms": 1.0,
                    "wall_samples_ms": samples, "cpu_parent_s": 1.0, "cpu_child_s": 1.0, "cpu_total_s": 2.0,
                    "jpeg_bytes_mean": 10.0, "jpeg_bytes_p95": 10.0, "quality": quality}

        aggregate = self.bench._aggregate([entry([1.0] * 19 + [100.0]), entry([50.0])])
        self.assertEqual(aggregate["wall_p95_ms"], 50.0)
        self.assertNotEqual(aggregate["wall_p95_ms"], 75.0)
        self.assertEqual(len(aggregate["wall_samples_ms"]), 21)
        self.assertEqual(len(aggregate["per_round"]), 2)


if __name__ == "__main__":
    unittest.main()
