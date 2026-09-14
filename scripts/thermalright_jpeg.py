"""Persistent Rust JPEG encoder client with a bounded stdio protocol.

Importing this stdlib-only module does not start an encoder process or load Qt,
GPU, TRCC, or sensor dependencies.
"""

import os
from pathlib import Path
import stat
import select
import struct
import subprocess
import threading
import time


FRAME_WIDTH = 1600
FRAME_HEIGHT = 720
RGBA8888_STRIDE = FRAME_WIDTH * 4
MAX_INPUT = RGBA8888_STRIDE * FRAME_HEIGHT
MAX_OUTPUT = 16 * 1024 * 1024
STDERR_TAIL_BYTES = 64 * 1024
STDERR_READ_CHUNK_BYTES = 8 * 1024


class ProtocolError(RuntimeError):
    pass


def validate_encoder_path(value):
    """Accept only a caller-owned executable below a private trusted artifact directory."""
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("--jpeg-encoder must be an absolute path without '..'")

    effective_owner = os.geteuid()
    private_ancestor = False
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            details = os.lstat(current)
        except OSError as exc:
            raise ValueError(f"--jpeg-encoder path is unavailable: {current}") from exc
        mode = details.st_mode
        if current == path:
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ValueError("--jpeg-encoder must be a regular file, not a symlink")
            if details.st_uid != effective_owner:
                raise ValueError("--jpeg-encoder must be owned by the effective user")
            if mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError("--jpeg-encoder must not be group- or other-writable")
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError(f"--jpeg-encoder parent is not a trusted directory: {current}")
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError(f"--jpeg-encoder parent is group- or other-writable: {current}")
        if details.st_uid not in (effective_owner, 0):
            raise ValueError(f"--jpeg-encoder parent has an untrusted owner: {current}")
        if details.st_uid == effective_owner and not mode & (stat.S_IRWXG | stat.S_IRWXO):
            private_ancestor = True

    if not private_ancestor:
        raise ValueError("--jpeg-encoder must be below a private effective-user artifact directory")
    if not os.access(path, os.X_OK):
        raise ValueError("--jpeg-encoder must be executable")
    return path


def rgba8888_payload(image, *, rgba_format):
    """Copy one tightly packed 1600x720 RGBA8888 image without importing Qt."""
    try:
        if image.isNull():
            raise ValueError("renderer returned a null or non-QImage frame")
        rgba = image.convertToFormat(rgba_format)
        if (
            rgba.isNull()
            or rgba.format() != rgba_format
            or (rgba.width(), rgba.height()) != (FRAME_WIDTH, FRAME_HEIGHT)
            or rgba.bytesPerLine() != RGBA8888_STRIDE
        ):
            raise ValueError("renderer returned an invalid RGBA8888 frame")
        payload = bytes(rgba.constBits())
    except AttributeError as exc:
        raise ValueError("renderer returned a null or non-QImage frame") from exc
    if len(payload) != MAX_INPUT:
        raise ValueError("RGBA8888 frame has an invalid byte length")
    return payload


def write_jpeg_response(jpeg, path):
    """Write only a bounded complete JPEG response, propagating every write failure."""
    if (
        not isinstance(jpeg, bytes)
        or len(jpeg) < 4
        or len(jpeg) > MAX_OUTPUT
        or not jpeg.startswith(b"\xff\xd8")
        or not jpeg.endswith(b"\xff\xd9")
    ):
        raise RuntimeError("Rust JPEG encoder returned an invalid response")
    with Path(path).open("wb", buffering=0) as output:
        remaining = memoryview(jpeg)
        while remaining:
            written = output.write(remaining)
            if isinstance(written, bool) or not isinstance(written, int) or written <= 0:
                raise OSError("short JPEG output write")
            remaining = remaining[written:]


def rust_jpeg_saver(session, *, rgba_format, payload_adapter=None):
    """Return a synchronous image-to-JPEG saver with an explicitly injected RGBA format."""
    adapter = payload_adapter or (lambda image: rgba8888_payload(image, rgba_format=rgba_format))

    def save(image, path):
        payload = adapter(image)
        if not isinstance(payload, bytes) or len(payload) != MAX_INPUT:
            raise ValueError("RGBA8888 frame has an invalid byte length")
        write_jpeg_response(session.request(payload), path)
        return True

    return save


def frame_request(payload):
    if len(payload) != MAX_INPUT:
        raise ProtocolError("invalid RGBA payload length")
    return struct.pack(">I", len(payload)) + payload


def parse_response(data):
    if len(data) < 4:
        raise ProtocolError("truncated JPEG response header")
    size, = struct.unpack(">I", data[:4])
    if size > MAX_OUTPUT:
        raise ProtocolError("JPEG response exceeds output limit")
    if len(data) != size + 4:
        raise ProtocolError("truncated JPEG response")
    return data[4:]


def proc_cpu_seconds(stat, ticks):
    try:
        fields = stat.rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / ticks
    except (IndexError, ValueError) as error:
        raise ValueError("invalid /proc stat") from error


def _wait(fd, readable, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("encoder pipe deadline expired")
    ready = select.select([fd] if readable else [], [] if readable else [fd], [], remaining)
    if not (ready[0] if readable else ready[1]):
        raise TimeoutError("encoder pipe deadline expired")


def _write_all(fd, data, deadline):
    view = memoryview(data)
    while view:
        _wait(fd, False, deadline)
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            continue
        if written <= 0:
            raise ProtocolError("encoder stdin closed during request")
        view = view[written:]


def _read_exact(fd, size, deadline):
    result = bytearray()
    while len(result) < size:
        _wait(fd, True, deadline)
        try:
            chunk = os.read(fd, size - len(result))
        except BlockingIOError:
            continue
        if not chunk:
            raise ProtocolError("encoder stdout closed during response")
        result.extend(chunk)
    return bytes(result)


class EncoderSession:
    def __init__(self, executable, subsampling, timeout_s, argv=None):
        self.timeout_s = timeout_s
        command = list(argv) if argv is not None else [str(executable), "--subsampling", subsampling]
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, close_fds=True,
        )
        self.stdin_fd, self.stdout_fd = self.process.stdin.fileno(), self.process.stdout.fileno()
        os.set_blocking(self.stdin_fd, False)
        os.set_blocking(self.stdout_fd, False)
        self.ticks = os.sysconf("SC_CLK_TCK")
        self._stderr_lock = threading.Lock()
        self._stderr_tail = bytearray()
        self._stderr_total_bytes = 0
        self._stderr_truncated = False
        self._stderr_reader = threading.Thread(target=self._drain_stderr, name="rust-jpeg-stderr", daemon=True)
        self._stderr_reader.start()

    def _drain_stderr(self):
        try:
            while True:
                chunk = os.read(self.process.stderr.fileno(), STDERR_READ_CHUNK_BYTES)
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr_total_bytes += len(chunk)
                    excess = len(self._stderr_tail) + len(chunk) - STDERR_TAIL_BYTES
                    if excess > 0:
                        del self._stderr_tail[:excess]
                        self._stderr_truncated = True
                    self._stderr_tail.extend(chunk)
        except OSError:
            # Pipe closure during bounded cleanup is expected and does not replace a protocol error.
            return

    def stderr_report(self):
        with self._stderr_lock:
            tail = bytes(self._stderr_tail)
            return {
                "tail_utf8": tail.decode("utf-8", "replace"),
                "tail_bytes": len(tail),
                "total_bytes": self._stderr_total_bytes,
                "truncated": self._stderr_truncated,
            }

    def _close_stream(self, stream):
        if stream and not stream.closed:
            try:
                stream.close()
            except OSError:
                pass

    def _finish_pipes(self):
        # The child has been reaped before this is called, so EOF should release the reader.
        self._stderr_reader.join(timeout=min(max(self.timeout_s, 0.05), 1.0))
        if self._stderr_reader.is_alive():
            self._close_stream(self.process.stderr)
            self._stderr_reader.join(timeout=0.1)
        else:
            self._close_stream(self.process.stderr)
        self._close_stream(self.process.stdout)

    def cpu_seconds(self):
        return proc_cpu_seconds(Path(f"/proc/{self.process.pid}/stat").read_text(), self.ticks)

    def request(self, rgba):
        if self.process.poll() is not None:
            self._finish_pipes()
            raise ProtocolError(self._child_error("encoder exited before request"))
        deadline = time.monotonic() + self.timeout_s
        try:
            _write_all(self.stdin_fd, struct.pack(">I", len(rgba)), deadline)
            _write_all(self.stdin_fd, rgba, deadline)
            size, = struct.unpack(">I", _read_exact(self.stdout_fd, 4, deadline))
            if size > MAX_OUTPUT:
                raise ProtocolError("JPEG response exceeds output limit")
            return _read_exact(self.stdout_fd, size, deadline)
        except (OSError, TimeoutError, ProtocolError) as error:
            self.abort()
            raise ProtocolError(self._child_error(str(error))) from error

    def _child_error(self, prefix):
        report = self.stderr_report()
        state = f"exit={self.process.returncode}" if self.process.poll() is not None else "encoder still running"
        stderr = report["tail_utf8"].strip() or "none"
        suffix = " (truncated tail)" if report["truncated"] else ""
        return f"{prefix}; {state}; stderr={stderr}{suffix}"

    def abort(self):
        """Stop and reap only this owned child; cleanup failures never mask the caller error."""
        self._close_stream(self.process.stdin)
        try:
            if self.process.poll() is None:
                try:
                    self.process.terminate()
                except OSError:
                    pass
                try:
                    self.process.wait(timeout=min(max(self.timeout_s, 0.05), 1.0))
                except (OSError, subprocess.TimeoutExpired):
                    if self.process.poll() is None:
                        try:
                            self.process.kill()
                            self.process.wait(timeout=1.0)
                        except (OSError, subprocess.TimeoutExpired):
                            pass
        finally:
            self._finish_pipes()

    def close(self):
        self._close_stream(self.process.stdin)
        try:
            self.process.wait(timeout=min(max(self.timeout_s, 0.05), 1.0))
        except subprocess.TimeoutExpired:
            self.abort()
        else:
            self._finish_pipes()
        if self.process.returncode:
            raise ProtocolError(self._child_error("encoder exited unsuccessfully"))
