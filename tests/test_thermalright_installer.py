#!/usr/bin/env python3
"""Black-box tests for the per-user Thermalright dashboard installer.

Every invocation gets an isolated HOME, venv, PATH, source checkout, and user
manager fake.  The real user manager and real home directory are never used.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import shutil
import stat
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SCRIPTS = PROJECT_ROOT / "scripts"
SOURCE_UNIT = PROJECT_ROOT / "systemd" / "thermalright-dashboard.service"
MODULE_NAMES = (
    "thermalright-dashboard.py",
    "thermalright_cinematic.py",
    "thermalright_cinematic_runtime.py",
    "thermalright_cinematic_gpu.py",
    "thermalright_jpeg.py",
)
LEGACY_MODULE_NAMES = MODULE_NAMES[:3]
HISTORICAL_V2_MODULE_NAMES = MODULE_NAMES[:4]
UNIT_NAME = "thermalright-dashboard.service"
MARKER = "# THERMALRIGHT_DASHBOARD_MANAGED_COPY"
ENCODER_DIR_NAME = "thermalright-dashboard-jpeg"
ENCODER_NAME = "rust-dashboard-jpeg"
ENCODER_MANIFEST_NAME = "rust-dashboard-jpeg.manifest"
ENCODER_MANIFEST_VERSION = "thermalright-dashboard-jpeg-v1"
QT_EXEC_START = (
    "ExecStart=%h/.local/share/rgb-naranja/trcc-venv/bin/python "
    "%h/.local/bin/thermalright-dashboard.py --continuous --renderer cinematic-gpu"
)
RUST_EXEC_START = f"{QT_EXEC_START} --jpeg-encoder %h/.local/share/rgb-naranja/{ENCODER_DIR_NAME}/{ENCODER_NAME}"


SYSTEMCTL_FAKE = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import signal
import sys

state_path = Path(os.environ["FAKE_SYSTEMCTL_STATE"])
event_path = Path(os.environ["FAKE_EVENT_LOG"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
if args[:1] != ["--user"]:
    raise SystemExit(97)
args = args[1:]
action = args[0]
event_path.write_text(event_path.read_text(encoding="utf-8") + f"systemctl:{action}\n", encoding="utf-8")

def save():
    state_path.write_text(json.dumps(state), encoding="utf-8")


def enabled_link() -> Path | None:
    value = os.environ.get("FAKE_ENABLED_LINK")
    return Path(value) if value else None


def signal_parent_if_requested() -> None:
    if os.environ.get("FAKE_SIGNAL_PARENT_ON_ACTION") == action:
        os.kill(os.getppid(), signal.SIGTERM)


if action == "is-active":
    print(state["active"])
    raise SystemExit(0 if state["active"] == "active" else 3)
if action == "is-enabled":
    print(state["enabled"])
    raise SystemExit(0 if state["enabled"] == "enabled" else 1)
if action == "daemon-reload":
    unit_target = os.environ.get("FAKE_UNIT_TARGET")
    if unit_target and not Path(unit_target).exists() and state["enabled"] == "disabled":
        state["enabled"] = "not-found"
        save()
    raise SystemExit(1 if os.environ.get("FAKE_FAIL_RELOAD") == "1" else 0)
if action == "stop":
    if os.environ.get("FAKE_FAIL_STOP") == "1":
        raise SystemExit(1)
    state.update(active="inactive", pid="0")
    save()
    raise SystemExit(0)
if action == "enable":
    state["enabled"] = "enabled"
    link = enabled_link()
    if link:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_text("THERMALRIGHT_DASHBOARD_MANAGED_ENABLEMENT\n", encoding="utf-8")
    save()
    signal_parent_if_requested()
    raise SystemExit(0)
if action == "disable":
    state["enabled"] = "disabled"
    link = enabled_link()
    if link and link.exists() and link.read_text(encoding="utf-8") == "THERMALRIGHT_DASHBOARD_MANAGED_ENABLEMENT\n":
        link.unlink()
    state.update(active="inactive", pid="0")
    save()
    signal_parent_if_requested()
    raise SystemExit(0)
if action == "start":
    if os.environ.get("FAKE_FAIL_START") == "1":
        state.update(active="failed", pid="0")
        save()
        raise SystemExit(1)
    state["starts"] += 1
    state.update(active="active", pid="200")
    save()
    signal_parent_if_requested()
    raise SystemExit(0)
if action == "show":
    property_name = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--property="))
    if property_name == "ActiveState":
        print(state["active"])
    elif property_name == "MainPID":
        state["pid_reads"] += 1
        if os.environ.get("FAKE_HEALTH_FAIL") == "1" and state["starts"] == 1 and state["pid_reads"] >= 2:
            print("201")
        else:
            print(state["pid"])
        save()
    else:
        raise SystemExit(98)
    raise SystemExit(0)
raise SystemExit(99)
'''


MV_FAKE = r'''#!/usr/bin/env bash
set -euo pipefail
last="${!#}"
destination="$(basename -- "$last")"
if [ "${FAKE_MV_FAIL_DEST:-}" = "$destination" ] \
    || { [ -n "${FAKE_MV_FAIL_DEST_PREFIX:-}" ] && [[ "$destination" == "${FAKE_MV_FAIL_DEST_PREFIX}"* ]]; }; then
    exit 1
fi
if [ -n "${FAKE_SIGNAL_PARENT_BEFORE_MV_DEST_PREFIX:-}" ] \
    && [[ "$destination" == "${FAKE_SIGNAL_PARENT_BEFORE_MV_DEST_PREFIX}"* ]]; then
    kill -TERM "$PPID"
    exit 0
fi
/usr/bin/mv "$@"
if [ "${FAKE_SIGNAL_PARENT_AT:-}" = "$destination" ]; then
    kill -TERM "$PPID"
fi
printf 'mv:%s\n' "$destination" >> "$FAKE_EVENT_LOG"
'''


SLEEP_FAKE = r'''#!/usr/bin/env bash
set -euo pipefail
printf 'sleep:%s\n' "$*" >> "$FAKE_EVENT_LOG"
'''


STAT_FAKE = r'''#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ge 4 ] && [ "$1" = "-c" ] && [ "$2" = "%u" ] && [ "$3" = "--" ] \
    && [ "${FAKE_FOREIGN_UID_PATH:-}" = "$4" ]; then
    printf '%s\n' "$(( $(/usr/bin/id -u) + 1 ))"
    exit 0
fi
exec /usr/bin/stat "$@"
'''


VENV_PYTHON_FAKE = r'''#!/usr/bin/env python3
import os
from pathlib import Path
import signal
import sys
if os.environ.get("FAKE_VENV_SENTINEL"):
    Path(os.environ["FAKE_VENV_SENTINEL"]).write_text("venv executed\n", encoding="utf-8")
if os.environ.get("FAKE_VENV_ORIGINAL_INVOCATION_SENTINEL"):
    Path(os.environ["FAKE_VENV_ORIGINAL_INVOCATION_SENTINEL"]).write_text(
        f"{sys.argv[0]}\n", encoding="utf-8"
    )
if os.environ.get("FAKE_SIGNAL_PARENT_FROM_VENV") == "1":
    os.kill(os.getppid(), signal.SIGTERM)
if len(sys.argv) >= 2 and sys.argv[1] == "-c":
    raise SystemExit(1 if os.environ.get("FAKE_DEPS") == "missing" else 0)
raise SystemExit(96)
'''


class ThermalrightInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.checkout = self.root / "checkout"
        self.scripts = self.checkout / "scripts"
        self.systemd = self.checkout / "systemd"
        self.scripts.mkdir(parents=True)
        self.systemd.mkdir()
        for name in ("install-thermalright-dashboard.sh", *MODULE_NAMES):
            shutil.copy2(SOURCE_SCRIPTS / name, self.scripts / name)
        shutil.copy2(SOURCE_UNIT, self.systemd / UNIT_NAME)
        (self.scripts / "install-thermalright-dashboard.sh").chmod(0o755)

        self.home = self.root / "fake home"
        self.home.mkdir()
        self.config = self.home / "config"
        self.fakebin = self.root / "fake bin"
        self.fakebin.mkdir()
        self.events = self.root / "events.log"
        self.events.write_text("", encoding="utf-8")
        self.state = self.root / "state.json"
        self.state.write_text(
            json.dumps({"active": "inactive", "enabled": "disabled", "pid": "0", "starts": 0, "pid_reads": 0}),
            encoding="utf-8",
        )
        self._write_executable("systemctl", SYSTEMCTL_FAKE)
        self._write_executable("mv", MV_FAKE)
        self._write_executable("sleep", SLEEP_FAKE)
        self._write_executable("stat", STAT_FAKE)

        self._write_venv_interpreter(self.venv_python)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @property
    def installer(self) -> Path:
        return self.scripts / "install-thermalright-dashboard.sh"

    @property
    def bin_dir(self) -> Path:
        return self.home / ".local" / "bin"

    @property
    def venv_bin_dir(self) -> Path:
        return self.home / ".local" / "share" / "rgb-naranja" / "trcc-venv" / "bin"

    @property
    def venv_python(self) -> Path:
        return self.venv_bin_dir / "python"

    @property
    def unit_target(self) -> Path:
        return self.config / "systemd" / "user" / UNIT_NAME

    @property
    def backup_root(self) -> Path:
        return self.home / ".local" / "share" / "rgb-naranja" / "dashboard-backups"

    @property
    def encoder_dir(self) -> Path:
        return self.home / ".local" / "share" / "rgb-naranja" / ENCODER_DIR_NAME

    @property
    def encoder_target(self) -> Path:
        return self.encoder_dir / ENCODER_NAME

    @property
    def encoder_manifest_target(self) -> Path:
        return self.encoder_dir / ENCODER_MANIFEST_NAME

    @property
    def enabled_link(self) -> Path:
        return self.config / "systemd" / "user" / "default.target.wants" / UNIT_NAME

    def _write_executable(self, name: str, content: str) -> None:
        path = self.fakebin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _write_venv_interpreter(self, path: Path, mode: int = 0o755) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(VENV_PYTHON_FAKE, encoding="utf-8")
        path.chmod(mode)

    def _replace_venv_python_with_symlink(self, target: Path | str) -> None:
        if self.venv_python.exists() or self.venv_python.is_symlink():
            self.venv_python.unlink()
        self.venv_python.symlink_to(target)

    def assert_venv_preflight_rejected(self, **extra_env: str) -> None:
        sentinel = self.root / "venv-executed"
        self.events.write_text("", encoding="utf-8")

        rejected = self.run_installer(FAKE_VENV_SENTINEL=str(sentinel), **extra_env)

        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse(sentinel.exists(), rejected.stderr)
        self.assertEqual(self.event_lines(), [])
        self.assertFalse(self.bin_dir.exists())
        self.assertFalse(self.config.exists())
        self.assertFalse(self.backup_root.exists())

    def run_installer(self, *args: str, timeout: float = 10, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
            "PATH": f"{self.fakebin}:/usr/bin:/bin",
            "FAKE_SYSTEMCTL_STATE": str(self.state),
            "FAKE_EVENT_LOG": str(self.events),
            "FAKE_UNIT_TARGET": str(self.unit_target),
            "FAKE_ENABLED_LINK": str(self.enabled_link),
            "LANG": "C",
            "LC_ALL": "C",
        }
        env.update(extra_env)
        return subprocess.run(
            [str(self.installer), *args],
            cwd=self.checkout,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )

    def event_lines(self) -> list[str]:
        return self.events.read_text(encoding="utf-8").splitlines()

    def backup_dir_from(self, result: subprocess.CompletedProcess[str]) -> Path:
        lines = [line for line in result.stdout.splitlines() if line.startswith("BACKUP_DIR=")]
        self.assertEqual(len(lines), 1, result.stdout)
        return Path(lines[0].split("=", 1)[1])

    def write_managed(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{MARKER}\n{contents}", encoding="utf-8")

    def write_old_bundle(self) -> dict[Path, str]:
        paths = [*(self.bin_dir / name for name in MODULE_NAMES), self.unit_target]
        original = {path: f"{MARKER}\nold {path.name} bytes\n" for path in paths}
        for path, contents in original.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        return original

    def write_encoder_source(self, name: str = "source encoder", payload: bytes = b"\x7fELFsynthetic-jpeg-encoder") -> Path:
        source = self.root / name
        source.write_bytes(payload)
        source.chmod(0o755)
        return source

    def assert_valid_encoder_pair(self, payload: bytes) -> None:
        self.assertEqual(self.encoder_target.read_bytes(), payload)
        self.assertEqual(stat.S_IMODE(self.encoder_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.encoder_target.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.encoder_manifest_target.stat().st_mode), 0o600)
        self.assertEqual(
            self.encoder_manifest_target.read_text(encoding="utf-8"),
            f"{ENCODER_MANIFEST_VERSION}\nsha256={hashlib.sha256(payload).hexdigest()}\nmode=0755\n",
        )

    def create_legacy_v1_backup(
        self, contents: dict[Path, str], *, name: str = "dashboard-backup-20250301T084434Z-gLGLIB"
    ) -> Path:
        """Create an untouched historical three-module v1 snapshot fixture."""
        backup_dir = self.backup_root / name
        backup_dir.mkdir(parents=True)
        backup_dir.chmod(0o700)
        labels = (*LEGACY_MODULE_NAMES, UNIT_NAME)
        targets = [*(self.bin_dir / name for name in LEGACY_MODULE_NAMES), self.unit_target]
        lines = [
            "thermalright-dashboard-backup-v1",
            "active_state=inactive",
            "enabled_state=disabled",
        ]
        for label, target in zip(labels, targets):
            value = contents.get(target)
            if value is None:
                lines.append(f"{label}|absent|-|-")
                continue
            backup_file = backup_dir / label
            backup_file.write_text(value, encoding="utf-8")
            backup_file.chmod(0o600)
            lines.append(
                f"{label}|present|644|{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
            )
        manifest = backup_dir / "manifest"
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest.chmod(0o600)
        return backup_dir

    def create_legacy_v2_backup(
        self, contents: dict[Path, str], *, name: str = "dashboard-backup-20260912T124000Z-etVeGs"
    ) -> Path:
        """Create an untouched historical four-module v2 snapshot fixture."""
        backup_dir = self.backup_root / name
        backup_dir.mkdir(parents=True)
        backup_dir.chmod(0o700)
        labels = (*HISTORICAL_V2_MODULE_NAMES, UNIT_NAME)
        targets = [*(self.bin_dir / name for name in HISTORICAL_V2_MODULE_NAMES), self.unit_target]
        lines = [
            "thermalright-dashboard-backup-v2",
            "active_state=inactive",
            "enabled_state=disabled",
        ]
        for label, target in zip(labels, targets):
            value = contents.get(target)
            if value is None:
                lines.append(f"{label}|absent|-|-")
                continue
            backup_file = backup_dir / label
            backup_file.write_text(value, encoding="utf-8")
            backup_file.chmod(0o600)
            lines.append(
                f"{label}|present|644|{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
            )
        manifest = backup_dir / "manifest"
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest.chmod(0o600)
        return backup_dir

    def assert_bundle_equals(self, original: dict[Path, str]) -> None:
        for path, contents in original.items():
            self.assertEqual(path.read_text(encoding="utf-8"), contents, path)

    def assert_no_transaction_temporaries(self) -> None:
        leftovers = [
            path
            for path in self.home.rglob("*")
            if ".new." in path.name or ".restore." in path.name
        ]
        self.assertEqual(leftovers, [])

    def test_default_install_publishes_the_managed_cinematic_bundle_without_activation(self) -> None:
        result = self.run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        backup_dir = self.backup_dir_from(result)
        self.assertTrue(backup_dir.is_dir())
        for name in MODULE_NAMES:
            installed = self.bin_dir / name
            self.assertTrue(installed.is_file(), name)
            self.assertIn(MARKER, installed.read_text(encoding="utf-8"), name)
        unit = self.unit_target.read_text(encoding="utf-8")
        self.assertIn(MARKER, unit)
        self.assertIn(QT_EXEC_START, unit)
        self.assertNotIn("--jpeg-encoder", unit)
        self.assertFalse(self.encoder_target.exists())
        self.assertFalse(self.encoder_manifest_target.exists())
        manifest = (backup_dir / "manifest").read_text(encoding="utf-8").splitlines()
        self.assertEqual(manifest[0], "thermalright-dashboard-backup-v3")
        self.assertEqual(len(manifest), 3 + len(MODULE_NAMES) + 1 + 2)
        self.assertIn("thermalright_cinematic_gpu.py|absent|-|-", manifest)
        self.assertIn("thermalright_jpeg.py|absent|-|-", manifest)
        self.assertIn(f"{ENCODER_NAME}|absent|-|-", manifest)
        self.assertIn(f"{ENCODER_MANIFEST_NAME}|absent|-|-", manifest)
        events = self.event_lines()
        self.assertEqual(events, [
            "systemctl:is-active",
            "systemctl:is-enabled",
            "mv:thermalright-dashboard.py",
            "mv:thermalright_cinematic.py",
            "mv:thermalright_cinematic_runtime.py",
            "mv:thermalright_cinematic_gpu.py",
            "mv:thermalright_jpeg.py",
            "mv:thermalright-dashboard.service",
            "systemctl:daemon-reload",
        ])

    def test_reruns_create_new_private_backups_without_overwriting_old_ones(self) -> None:
        first = self.run_installer()
        self.assertEqual(first.returncode, 0, first.stderr)
        first_backup = self.backup_dir_from(first)
        first_manifest = (first_backup / "manifest").read_bytes()
        self.assertEqual(stat.S_IMODE(first_backup.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((first_backup / "manifest").stat().st_mode), 0o600)

        second = self.run_installer()
        self.assertEqual(second.returncode, 0, second.stderr)
        second_backup = self.backup_dir_from(second)

        self.assertNotEqual(first_backup, second_backup)
        self.assertEqual((first_backup / "manifest").read_bytes(), first_manifest)
        self.assertEqual({path.name for path in self.backup_root.iterdir()}, {first_backup.name, second_backup.name})

    def test_unsafe_config_roots_are_rejected_before_venv_or_output_creation(self) -> None:
        linked_outside = self.root / "outside config root"
        (linked_outside / "nested").mkdir(parents=True)
        linked_ancestor = self.home / "config link"
        linked_ancestor.symlink_to(linked_outside, target_is_directory=True)
        lexical_parent = self.home / "lexical parent"
        lexical_parent.mkdir()
        lexical_outside = self.home / "lexical outside"
        cases = (
            ("relative", "relative config", self.checkout / "relative config"),
            ("lexical", f"{lexical_parent}/../{lexical_outside.name}", lexical_outside),
            ("symlink", str(linked_ancestor / "nested" / "config"), linked_outside / "nested" / "config"),
        )

        for name, config_root, output_root in cases:
            with self.subTest(name=name):
                sentinel = self.root / f"{name}-venv-executed"
                self.events.write_text("", encoding="utf-8")

                rejected = self.run_installer(
                    XDG_CONFIG_HOME=config_root,
                    FAKE_VENV_SENTINEL=str(sentinel),
                )

                self.assertNotEqual(rejected.returncode, 0)
                self.assertFalse(sentinel.exists(), rejected.stderr)
                self.assertFalse((output_root / "systemd" / "user").exists())
                self.assertEqual(self.event_lines(), [])

    def test_unsafe_home_ownership_or_writability_is_rejected_before_venv_or_mkdir(self) -> None:
        original_mode = stat.S_IMODE(self.home.stat().st_mode)
        cases = (
            ("foreign-owner", {"FAKE_FOREIGN_UID_PATH": str(self.home)}),
            ("group-writable", {}),
        )

        for name, extra_env in cases:
            with self.subTest(name=name):
                sentinel = self.root / f"{name}-venv-executed"
                self.events.write_text("", encoding="utf-8")
                if name == "group-writable":
                    self.home.chmod(0o770)
                try:
                    rejected = self.run_installer(
                        FAKE_VENV_SENTINEL=str(sentinel),
                        **extra_env,
                    )
                finally:
                    self.home.chmod(original_mode)

                self.assertNotEqual(rejected.returncode, 0)
                self.assertFalse(sentinel.exists(), rejected.stderr)
                self.assertFalse(self.config.exists())
                self.assertEqual(self.event_lines(), [])

    def test_venv_interpreter_leaf_symlink_chain_is_accepted_and_invokes_original_path(self) -> None:
        terminal = self.root / "trusted fake interpreter"
        self._write_venv_interpreter(terminal)
        self._replace_venv_python_with_symlink("python3")
        (self.venv_bin_dir / "python3").symlink_to(terminal)
        invocation = self.root / "original-venv-invocation"

        installed = self.run_installer(
            FAKE_VENV_ORIGINAL_INVOCATION_SENTINEL=str(invocation),
        )

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(invocation.read_text(encoding="utf-8"), f"{self.venv_python}\n")
        self.assertNotEqual(invocation.read_text(encoding="utf-8").strip(), str(terminal))

    def test_venv_interpreter_rejections_happen_before_execution_or_output(self) -> None:
        def replace_terminal(name: str, mode: int = 0o755) -> Path:
            terminal = self.root / "terminals" / name / "python"
            self._write_venv_interpreter(terminal, mode)
            self._replace_venv_python_with_symlink(terminal)
            return terminal

        def reset_venv() -> None:
            for leaf in (self.venv_python, self.venv_bin_dir / "python3"):
                if leaf.exists() or leaf.is_symlink():
                    leaf.unlink()
            self._write_venv_interpreter(self.venv_python)

        def dangling() -> dict[str, str]:
            self._replace_venv_python_with_symlink("missing-python")
            return {}

        def cycle() -> dict[str, str]:
            self._replace_venv_python_with_symlink("python3")
            (self.venv_bin_dir / "python3").symlink_to("python")
            return {}

        def symlinked_target_parent() -> dict[str, str]:
            real_parent = self.root / "real terminal parent"
            terminal = real_parent / "python"
            self._write_venv_interpreter(terminal)
            linked_parent = self.root / "linked terminal parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            self._replace_venv_python_with_symlink(linked_parent / "python")
            return {}

        def writable_link_parent() -> dict[str, str]:
            terminal = replace_terminal("writable-link-parent")
            terminal.parent.chmod(0o775)
            return {}

        def foreign_link_parent() -> dict[str, str]:
            terminal = replace_terminal("foreign-link-parent")
            return {"FAKE_FOREIGN_UID_PATH": str(terminal.parent)}

        def foreign_terminal() -> dict[str, str]:
            terminal = replace_terminal("foreign-terminal")
            return {"FAKE_FOREIGN_UID_PATH": str(terminal)}

        def writable_terminal() -> dict[str, str]:
            replace_terminal("writable-terminal", 0o775)
            return {}

        def nonexecutable_terminal() -> dict[str, str]:
            replace_terminal("nonexecutable-terminal", 0o644)
            return {}

        cases = [
            ("dangling", dangling),
            ("cycle", cycle),
            ("symlinked-target-parent", symlinked_target_parent),
            ("writable-link-parent", writable_link_parent),
            ("foreign-link-parent", foreign_link_parent),
            ("foreign-terminal", foreign_terminal),
            ("writable-terminal", writable_terminal),
            ("nonexecutable-terminal", nonexecutable_terminal),
        ]
        for name, configure in cases:
            with self.subTest(name=name):
                reset_venv()
                extra_env = configure()
                self.assert_venv_preflight_rejected(**extra_env)

    def test_missing_dependencies_or_bad_sources_fail_before_stopping_the_service(self) -> None:
        missing_deps = self.run_installer("--start", FAKE_DEPS="missing")
        self.assertNotEqual(missing_deps.returncode, 0)
        self.assertNotIn("systemctl:stop", self.event_lines())

        self.events.write_text("", encoding="utf-8")
        cinematic_source = self.scripts / "thermalright_cinematic.py"
        cinematic_source.unlink()
        cinematic_source.symlink_to(self.scripts / "thermalright-dashboard.py")
        bad_source = self.run_installer("--start")
        self.assertNotEqual(bad_source.returncode, 0)
        self.assertNotIn("systemctl:stop", self.event_lines())

    def test_unowned_or_symlinked_targets_are_refused_before_service_mutation(self) -> None:
        targets = [*(self.bin_dir / name for name in MODULE_NAMES), self.unit_target]
        for kind in ("unowned", "symlink"):
            for target in targets:
                with self.subTest(kind=kind, target=target.name):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if kind == "unowned":
                        target.write_text("personal file\n", encoding="utf-8")
                    else:
                        outside = self.root / f"outside-{target.name}"
                        outside.write_text("personal file\n", encoding="utf-8")
                        target.symlink_to(outside)

                    result = self.run_installer("--start")

                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("systemctl:stop", self.event_lines())
                    if kind == "unowned":
                        self.assertEqual(target.read_text(encoding="utf-8"), "personal file\n")
                    else:
                        self.assertTrue(target.is_symlink())
                    self.events.write_text("", encoding="utf-8")
                    target.unlink()

    def test_foreign_owned_gpu_target_is_refused_before_stop_and_preserved(self) -> None:
        gpu_target = self.bin_dir / "thermalright_cinematic_gpu.py"
        self.write_managed(gpu_target, "managed but foreign owned\n")
        self.state.write_text(
            json.dumps({"active": "active", "enabled": "enabled", "pid": "111", "starts": 0, "pid_reads": 0}),
            encoding="utf-8",
        )

        rejected = self.run_installer("--start", FAKE_FOREIGN_UID_PATH=str(gpu_target))

        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(gpu_target.read_text(encoding="utf-8"), f"{MARKER}\nmanaged but foreign owned\n")
        self.assertNotIn("systemctl:stop", self.event_lines())

    def test_start_stops_before_publication_and_starts_only_after_the_complete_bundle(self) -> None:
        self.state.write_text(
            json.dumps({"active": "active", "enabled": "enabled", "pid": "111", "starts": 0, "pid_reads": 0}),
            encoding="utf-8",
        )

        result = self.run_installer("--start")

        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.event_lines()
        stop_index = events.index("systemctl:stop")
        publish_indices = [events.index(f"mv:{name}") for name in (*MODULE_NAMES, UNIT_NAME)]
        start_index = events.index("systemctl:start")
        self.assertLess(stop_index, min(publish_indices))
        self.assertLess(max(publish_indices), start_index)
        self.assertIn("sleep:3", events)

    def test_start_failure_restores_old_bytes_absent_modules_and_original_activation(self) -> None:
        old_dashboard = "old dashboard bytes\n"
        old_unit = "old unit bytes\n"
        self.write_managed(self.bin_dir / "thermalright-dashboard.py", old_dashboard)
        self.write_managed(self.unit_target, old_unit)
        self.state.write_text(
            json.dumps({"active": "active", "enabled": "enabled", "pid": "111", "starts": 0, "pid_reads": 0}),
            encoding="utf-8",
        )

        result = self.run_installer("--start", FAKE_HEALTH_FAIL="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.bin_dir / "thermalright-dashboard.py").read_text(encoding="utf-8"), f"{MARKER}\n{old_dashboard}")
        self.assertEqual(self.unit_target.read_text(encoding="utf-8"), f"{MARKER}\n{old_unit}")
        self.assertFalse((self.bin_dir / "thermalright_cinematic.py").exists())
        self.assertFalse((self.bin_dir / "thermalright_cinematic_runtime.py").exists())
        self.assertFalse((self.bin_dir / "thermalright_cinematic_gpu.py").exists())
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual((state["active"], state["enabled"]), ("active", "enabled"))

    def test_partial_publish_failure_restores_the_prior_bundle_without_starting_it(self) -> None:
        self.write_managed(self.bin_dir / "thermalright-dashboard.py", "old dashboard\n")
        self.write_managed(self.unit_target, "old unit\n")

        result = self.run_installer(FAKE_MV_FAIL_DEST="thermalright_cinematic_gpu.py")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.bin_dir / "thermalright-dashboard.py").read_text(encoding="utf-8"), f"{MARKER}\nold dashboard\n")
        self.assertEqual(self.unit_target.read_text(encoding="utf-8"), f"{MARKER}\nold unit\n")
        self.assertFalse((self.bin_dir / "thermalright_cinematic.py").exists())
        self.assertFalse((self.bin_dir / "thermalright_cinematic_runtime.py").exists())
        self.assertFalse((self.bin_dir / "thermalright_cinematic_gpu.py").exists())
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["active"], "inactive")
        self.assertNotIn("systemctl:start", self.event_lines())

    def test_upgrade_from_three_modules_records_gpu_and_client_absence_in_a_v3_snapshot_without_gl_boot(self) -> None:
        legacy_targets = [*(self.bin_dir / name for name in LEGACY_MODULE_NAMES), self.unit_target]
        for target in legacy_targets:
            self.write_managed(target, f"legacy {target.name}\n")

        installed = self.run_installer()

        self.assertEqual(installed.returncode, 0, installed.stderr)
        backup_dir = self.backup_dir_from(installed)
        manifest = (backup_dir / "manifest").read_text(encoding="utf-8").splitlines()
        self.assertEqual(manifest[0], "thermalright-dashboard-backup-v3")
        self.assertIn("thermalright_cinematic_gpu.py|absent|-|-", manifest)
        self.assertIn("thermalright_jpeg.py|absent|-|-", manifest)
        self.assertTrue((self.bin_dir / "thermalright_cinematic_gpu.py").is_file())
        self.assertNotIn("systemctl:start", self.event_lines())

    def test_v3_rollback_removes_only_the_managed_gpu_added_after_an_absent_snapshot(self) -> None:
        installed = self.run_installer()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        backup_dir = self.backup_dir_from(installed)
        gpu_target = self.bin_dir / "thermalright_cinematic_gpu.py"
        self.assertTrue(gpu_target.is_file())
        self.events.write_text("", encoding="utf-8")

        rolled_back = self.run_installer("--rollback", str(backup_dir))

        self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
        self.assertFalse(gpu_target.exists())
        self.assertIn("systemctl:stop", self.event_lines())

    def test_legacy_v1_rollback_restores_cpu_bundle_and_removes_only_managed_new_gpu(self) -> None:
        legacy_paths = [*(self.bin_dir / name for name in LEGACY_MODULE_NAMES), self.unit_target]
        legacy_contents = {
            path: f"{MARKER}\nlegacy {path.name}\n"
            for path in legacy_paths
        }
        legacy_backup = self.create_legacy_v1_backup(legacy_contents)
        manifest_before = (legacy_backup / "manifest").read_bytes()

        installed = self.run_installer()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        gpu_target = self.bin_dir / "thermalright_cinematic_gpu.py"
        self.assertTrue(gpu_target.is_file())
        self.events.write_text("", encoding="utf-8")

        rolled_back = self.run_installer("--rollback", str(legacy_backup))

        self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
        self.assert_bundle_equals(legacy_contents)
        self.assertFalse(gpu_target.exists())
        self.assertEqual((legacy_backup / "manifest").read_bytes(), manifest_before)
        self.assertIn("systemctl:stop", self.event_lines())

    def test_legacy_v1_rollback_refuses_unmanaged_or_linked_gpu_before_stop(self) -> None:
        legacy_paths = [*(self.bin_dir / name for name in LEGACY_MODULE_NAMES), self.unit_target]
        legacy_contents = {path: f"{MARKER}\nlegacy {path.name}\n" for path in legacy_paths}
        gpu_target = self.bin_dir / "thermalright_cinematic_gpu.py"
        for kind in ("unmanaged", "linked"):
            with self.subTest(kind=kind):
                legacy_backup = self.create_legacy_v1_backup(
                    legacy_contents,
                    name=f"dashboard-backup-20250301T084434Z-legacy-gpu-{kind}",
                )
                installed = self.run_installer()
                self.assertEqual(installed.returncode, 0, installed.stderr)
                gpu_target.unlink()
                if kind == "unmanaged":
                    gpu_target.write_text("personal GPU renderer\n", encoding="utf-8")
                else:
                    personal_gpu = self.root / "personal-gpu-renderer"
                    personal_gpu.write_text("personal GPU renderer\n", encoding="utf-8")
                    gpu_target.symlink_to(personal_gpu)
                self.events.write_text("", encoding="utf-8")

                rejected = self.run_installer("--rollback", str(legacy_backup))

                self.assertNotEqual(rejected.returncode, 0)
                self.assertNotIn("systemctl:stop", self.event_lines())
                if kind == "unmanaged":
                    self.assertEqual(gpu_target.read_text(encoding="utf-8"), "personal GPU renderer\n")
                else:
                    self.assertTrue(gpu_target.is_symlink())
                gpu_target.unlink()

    def test_malformed_v1_and_v2_manifests_refuse_before_stop(self) -> None:
        legacy_paths = [*(self.bin_dir / name for name in LEGACY_MODULE_NAMES), self.unit_target]
        legacy_contents = {path: f"{MARKER}\nlegacy {path.name}\n" for path in legacy_paths}
        for version in ("v1", "v2"):
            for defect in ("version", "count", "path", "hash", "owner", "symlink"):
                with self.subTest(version=version, defect=defect):
                    if version == "v1":
                        backup_dir = self.create_legacy_v1_backup(
                            legacy_contents,
                            name=f"dashboard-backup-20250301T084434Z-legacy-{defect}",
                        )
                    else:
                        historical_paths = [*(self.bin_dir / name for name in HISTORICAL_V2_MODULE_NAMES), self.unit_target]
                        historical_contents = {path: f"{MARKER}\nhistorical {path.name}\n" for path in historical_paths}
                        backup_dir = self.create_legacy_v2_backup(
                            historical_contents,
                            name=f"dashboard-backup-20260912T124000Z-etVeGs-{defect}",
                        )
                    manifest = backup_dir / "manifest"
                    contents = manifest.read_text(encoding="utf-8")
                    extra_env: dict[str, str] = {}
                    if defect == "version":
                        manifest.write_text(contents.replace(f"thermalright-dashboard-backup-{version}", "unknown", 1), encoding="utf-8")
                    elif defect == "count":
                        manifest.write_text(contents + "unexpected-entry\n", encoding="utf-8")
                    elif defect == "path":
                        manifest.write_text(contents.replace("thermalright-dashboard.py", "foreign.py", 1), encoding="utf-8")
                    elif defect == "hash":
                        lines = contents.splitlines()
                        line_index = next(index for index, line in enumerate(lines) if "|present|" in line)
                        lines[line_index] = lines[line_index].rsplit("|", 1)[0] + "|" + ("0" * 64)
                        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    elif defect == "owner":
                        extra_env["FAKE_FOREIGN_UID_PATH"] = str(manifest)
                    else:
                        outside_manifest = self.root / f"outside-{version}-{defect}"
                        outside_manifest.write_text(contents, encoding="utf-8")
                        manifest.unlink()
                        manifest.symlink_to(outside_manifest)
                    self.events.write_text("", encoding="utf-8")

                    rejected = self.run_installer("--rollback", str(backup_dir), **extra_env)

                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertNotIn("systemctl:stop", self.event_lines())

    def test_rollback_refuses_escaped_or_tampered_backup_without_deleting_unowned_files(self) -> None:
        installed = self.run_installer()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        backup_dir = self.backup_dir_from(installed)
        cinematic_target = self.bin_dir / "thermalright_cinematic.py"
        cinematic_target.unlink()
        cinematic_target.write_text("personal file\n", encoding="utf-8")
        self.events.write_text("", encoding="utf-8")

        unowned = self.run_installer("--rollback", str(backup_dir))
        self.assertNotEqual(unowned.returncode, 0)
        self.assertEqual(cinematic_target.read_text(encoding="utf-8"), "personal file\n")
        self.assertNotIn("systemctl:stop", self.event_lines())

        self.events.write_text("", encoding="utf-8")
        escaped = self.run_installer("--rollback", str(self.root / "outside-backup"))
        self.assertNotEqual(escaped.returncode, 0)
        self.assertNotIn("systemctl:stop", self.event_lines())

        cinematic_target.unlink()
        self.write_managed(cinematic_target, "managed again\n")
        manifest = backup_dir / "manifest"
        outside_manifest = self.root / "outside-manifest"
        outside_manifest.write_text("not a backup\n", encoding="utf-8")
        manifest.unlink()
        manifest.symlink_to(outside_manifest)
        self.events.write_text("", encoding="utf-8")
        symlinked_backup = self.run_installer("--rollback", str(backup_dir))
        self.assertNotEqual(symlinked_backup.returncode, 0)
        self.assertNotIn("systemctl:stop", self.event_lines())

    def test_signal_before_transaction_arming_leaves_the_old_bundle_and_service_state_untouched(self) -> None:
        original = self.write_old_bundle()
        self.state.write_text(
            json.dumps({"active": "active", "enabled": "enabled", "pid": "111", "starts": 0, "pid_reads": 0}),
            encoding="utf-8",
        )

        result = self.run_installer("--start", timeout=5, FAKE_SIGNAL_PARENT_FROM_VENV="1")

        self.assertNotEqual(result.returncode, 0)
        self.assert_bundle_equals(original)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual((state["active"], state["enabled"]), ("active", "enabled"))
        self.assertNotIn("systemctl:stop", self.event_lines())
        self.assert_no_transaction_temporaries()

    def test_signal_during_first_publish_restores_the_old_bundle_and_service_state(self) -> None:
        original = self.write_old_bundle()
        self.state.write_text(
            json.dumps({"active": "active", "enabled": "enabled", "pid": "111", "starts": 0, "pid_reads": 0}),
            encoding="utf-8",
        )

        result = self.run_installer(
            "--start",
            timeout=5,
            FAKE_SIGNAL_PARENT_AT="thermalright-dashboard.py",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assert_bundle_equals(original)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual((state["active"], state["enabled"]), ("active", "enabled"))
        self.assert_no_transaction_temporaries()

    def test_signal_during_activation_restores_the_old_bundle_and_service_state(self) -> None:
        original = self.write_old_bundle()
        self.state.write_text(
            json.dumps({"active": "active", "enabled": "enabled", "pid": "111", "starts": 0, "pid_reads": 0}),
            encoding="utf-8",
        )

        result = self.run_installer("--start", timeout=5, FAKE_SIGNAL_PARENT_ON_ACTION="start")

        self.assertNotEqual(result.returncode, 0)
        self.assert_bundle_equals(original)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual((state["active"], state["enabled"]), ("active", "enabled"))
        self.assert_no_transaction_temporaries()

    def test_signal_during_manual_rollback_finishes_the_selected_snapshot(self) -> None:
        original = self.write_old_bundle()
        self.state.write_text(
            json.dumps({"active": "active", "enabled": "enabled", "pid": "111", "starts": 0, "pid_reads": 0}),
            encoding="utf-8",
        )
        installed = self.run_installer()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        backup_dir = self.backup_dir_from(installed)
        self.events.write_text("", encoding="utf-8")

        result = self.run_installer(
            "--rollback",
            str(backup_dir),
            timeout=5,
            FAKE_SIGNAL_PARENT_AT="thermalright-dashboard.py",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assert_bundle_equals(original)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual((state["active"], state["enabled"]), ("active", "enabled"))
        self.assert_no_transaction_temporaries()

    def test_first_start_rollback_restores_not_found_enablement_without_a_managed_link(self) -> None:
        self.state.write_text(
            json.dumps({"active": "inactive", "enabled": "not-found", "pid": "0", "starts": 0, "pid_reads": 0}),
            encoding="utf-8",
        )
        started = self.run_installer("--start")
        self.assertEqual(started.returncode, 0, started.stderr)
        backup_dir = self.backup_dir_from(started)
        self.assertTrue(self.enabled_link.is_file())

        rolled_back = self.run_installer("--rollback", str(backup_dir))

        self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
        self.assertFalse(any((self.bin_dir / name).exists() for name in MODULE_NAMES))
        self.assertFalse(self.unit_target.exists())
        self.assertFalse(self.enabled_link.exists())
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual((state["active"], state["enabled"]), ("inactive", "not-found"))

    def test_rollback_rejects_a_foreign_owned_backup_before_service_mutation(self) -> None:
        installed = self.run_installer()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        backup_dir = self.backup_dir_from(installed)
        before = {path: path.read_text(encoding="utf-8") for path in [*(self.bin_dir / name for name in MODULE_NAMES), self.unit_target]}
        self.events.write_text("", encoding="utf-8")

        rejected = self.run_installer("--rollback", str(backup_dir), FAKE_FOREIGN_UID_PATH=str(backup_dir / "manifest"))

        self.assertNotEqual(rejected.returncode, 0)
        self.assert_bundle_equals(before)
        self.assertNotIn("systemctl:stop", self.event_lines())

    def test_uninstall_removes_only_the_complete_managed_bundle_and_keeps_backups(self) -> None:
        installed = self.run_installer()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        backup_dir = self.backup_dir_from(installed)
        self.events.write_text("", encoding="utf-8")

        removed = self.run_installer("--uninstall")

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(any((self.bin_dir / name).exists() for name in MODULE_NAMES))
        self.assertFalse(self.unit_target.exists())
        self.assertTrue(backup_dir.is_dir())
        self.assertIn("systemctl:disable", self.event_lines())

    def test_failed_unit_transform_cleans_only_its_owned_tempfile(self) -> None:
        source = self.write_encoder_source()
        unit_dir = self.unit_target.parent
        unit_dir.mkdir(parents=True)
        unrelated = unit_dir / f".{UNIT_NAME}.transform.unrelated"
        unrelated.write_text("keep this unrelated tempfile\n", encoding="utf-8")

        failed = self.run_installer(
            "--jpeg-encoder",
            str(source),
            FAKE_MV_FAIL_DEST_PREFIX=f".{UNIT_NAME}.new.",
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(
            [path for path in unit_dir.glob(f".{UNIT_NAME}.transform.*") if path != unrelated],
            [],
        )
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep this unrelated tempfile\n")
        self.assertEqual(self.event_lines(), [])

    def test_signal_during_unit_transform_cleans_its_owned_tempfile(self) -> None:
        source = self.write_encoder_source()
        unit_dir = self.unit_target.parent
        unit_dir.mkdir(parents=True)
        unrelated = unit_dir / f".{UNIT_NAME}.transform.signal-sentinel"
        unrelated.write_text("keep this unrelated tempfile\n", encoding="utf-8")

        interrupted = self.run_installer(
            "--jpeg-encoder",
            str(source),
            FAKE_SIGNAL_PARENT_BEFORE_MV_DEST_PREFIX=f".{UNIT_NAME}.new.",
        )

        self.assertNotEqual(interrupted.returncode, 0)
        self.assertEqual(
            [path for path in unit_dir.glob(f".{UNIT_NAME}.transform.*") if path != unrelated],
            [],
        )
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep this unrelated tempfile\n")
        self.assertEqual(self.event_lines(), [])

    def test_jpeg_encoder_opt_in_installs_a_private_verified_pair_and_exact_unit_argument(self) -> None:
        source = self.write_encoder_source("source encoder with spaces")
        sentinel = self.root / "encoder-was-executed"

        result = self.run_installer("--jpeg-encoder", str(source))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_valid_encoder_pair(source.read_bytes())
        self.assertFalse(sentinel.exists())
        unit = self.unit_target.read_text(encoding="utf-8")
        self.assertEqual(unit.count("ExecStart="), 1)
        self.assertIn(RUST_EXEC_START, unit)
        self.assertNotIn(str(self.home), unit)
        self.assertNotIn("--jpeg-encoder " + str(source), unit)
        self.assertIn(QT_EXEC_START, (self.systemd / UNIT_NAME).read_text(encoding="utf-8"))
        manifest = (self.backup_dir_from(result) / "manifest").read_text(encoding="utf-8")
        self.assertIn(f"{ENCODER_NAME}|absent|-|-", manifest)
        self.assertIn(f"{ENCODER_MANIFEST_NAME}|absent|-|-", manifest)

    def test_default_qt_install_preserves_an_existing_valid_encoder_pair_and_snapshots_it(self) -> None:
        source = self.write_encoder_source()
        first = self.run_installer("--jpeg-encoder", str(source))
        self.assertEqual(first.returncode, 0, first.stderr)
        original_binary = self.encoder_target.read_bytes()
        original_metadata = self.encoder_manifest_target.read_bytes()

        default = self.run_installer()

        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(self.encoder_target.read_bytes(), original_binary)
        self.assertEqual(self.encoder_manifest_target.read_bytes(), original_metadata)
        unit = self.unit_target.read_text(encoding="utf-8")
        self.assertIn(QT_EXEC_START, unit)
        self.assertNotIn("--jpeg-encoder", unit)
        manifest = (self.backup_dir_from(default) / "manifest").read_text(encoding="utf-8")
        self.assertIn(f"{ENCODER_NAME}|present|755|{hashlib.sha256(original_binary).hexdigest()}", manifest)

    def test_jpeg_encoder_cli_and_source_rejections_happen_before_service_stop(self) -> None:
        source = self.write_encoder_source()
        unsafe = self.write_encoder_source("unsafe encoder")
        unsafe.chmod(0o775)
        malformed = self.write_encoder_source("not an elf", b"not-an-elf")
        linked = self.root / "linked encoder"
        linked.symlink_to(source)
        cases = (
            ("--jpeg-encoder",),
            ("--jpeg-encoder", "relative"),
            ("--jpeg-encoder", str(source), "--jpeg-encoder", str(source)),
            ("--uninstall", "--jpeg-encoder", str(source)),
            ("--start", "--jpeg-encoder", str(unsafe)),
            ("--start", "--jpeg-encoder", str(malformed)),
            ("--start", "--jpeg-encoder", str(linked)),
        )
        for args in cases:
            with self.subTest(args=args):
                self.events.write_text("", encoding="utf-8")
                rejected = self.run_installer(*args)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertNotIn("systemctl:stop", self.event_lines())

    def test_jpeg_encoder_rejects_an_ambiguous_or_nonmatching_source_unit_before_stop(self) -> None:
        source = self.write_encoder_source()
        unit_source = self.systemd / UNIT_NAME
        original = unit_source.read_text(encoding="utf-8")
        for contents in (
            original.replace(QT_EXEC_START, "ExecStart=/foreign-dashboard", 1),
            original + QT_EXEC_START + "\n",
        ):
            with self.subTest(contents=contents):
                unit_source.write_text(contents, encoding="utf-8")
                self.events.write_text("", encoding="utf-8")
                rejected = self.run_installer("--start", "--jpeg-encoder", str(source))
                self.assertNotEqual(rejected.returncode, 0)
                self.assertNotIn("systemctl:stop", self.event_lines())
        unit_source.write_text(original, encoding="utf-8")

    def test_invalid_or_partial_encoder_pair_refuses_before_stop_and_preserves_files(self) -> None:
        source = self.write_encoder_source()
        installed = self.run_installer("--jpeg-encoder", str(source))
        self.assertEqual(installed.returncode, 0, installed.stderr)
        original_binary = self.encoder_target.read_bytes()
        self.encoder_manifest_target.unlink()
        self.state.write_text(
            json.dumps({"active": "active", "enabled": "enabled", "pid": "111", "starts": 0, "pid_reads": 0}),
            encoding="utf-8",
        )
        self.events.write_text("", encoding="utf-8")

        rejected = self.run_installer("--start")

        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.encoder_target.read_bytes(), original_binary)
        self.assertFalse(self.encoder_manifest_target.exists())
        self.assertNotIn("systemctl:stop", self.event_lines())

    def test_foreign_symlinked_or_malformed_encoder_pairs_refuse_before_service_stop(self) -> None:
        source = self.write_encoder_source()
        installed = self.run_installer("--jpeg-encoder", str(source))
        self.assertEqual(installed.returncode, 0, installed.stderr)
        binary = self.encoder_target.read_bytes()
        metadata = self.encoder_manifest_target.read_bytes()
        outside = self.root / "foreign encoder metadata"
        outside.write_bytes(metadata)
        cases = ("foreign", "missing", "hash", "symlink", "extra")
        for defect in cases:
            with self.subTest(defect=defect):
                if self.encoder_manifest_target.exists() or self.encoder_manifest_target.is_symlink():
                    self.encoder_manifest_target.unlink()
                self.encoder_target.write_bytes(binary)
                self.encoder_target.chmod(0o755)
                self.encoder_manifest_target.write_bytes(metadata)
                self.encoder_manifest_target.chmod(0o600)
                extra_env: dict[str, str] = {}
                if defect == "foreign":
                    extra_env["FAKE_FOREIGN_UID_PATH"] = str(self.encoder_target)
                elif defect == "missing":
                    self.encoder_manifest_target.unlink()
                elif defect == "hash":
                    self.encoder_manifest_target.write_text(
                        f"{ENCODER_MANIFEST_VERSION}\nsha256={'0' * 64}\nmode=0755\n", encoding="utf-8"
                    )
                elif defect == "symlink":
                    self.encoder_manifest_target.unlink()
                    self.encoder_manifest_target.symlink_to(outside)
                else:
                    self.encoder_manifest_target.write_bytes(metadata + b"path=../outside\n")
                self.events.write_text("", encoding="utf-8")

                rejected = self.run_installer("--start", **extra_env)

                self.assertNotEqual(rejected.returncode, 0)
                self.assertNotIn("systemctl:stop", self.event_lines())

    def test_v1_and_v2_rollbacks_remove_only_a_valid_managed_encoder_pair_and_client(self) -> None:
        source = self.write_encoder_source()
        for version, names, creator in (
            ("v1", LEGACY_MODULE_NAMES, self.create_legacy_v1_backup),
            ("v2", HISTORICAL_V2_MODULE_NAMES, self.create_legacy_v2_backup),
        ):
            with self.subTest(version=version):
                paths = [*(self.bin_dir / name for name in names), self.unit_target]
                old = {path: f"{MARKER}\nold {path.name}\n" for path in paths}
                backup = creator(old, name=f"dashboard-backup-20260912T124000Z-{version}-pair")
                installed = self.run_installer("--jpeg-encoder", str(source))
                self.assertEqual(installed.returncode, 0, installed.stderr)
                self.events.write_text("", encoding="utf-8")

                rolled_back = self.run_installer("--rollback", str(backup))

                self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
                self.assert_bundle_equals(old)
                self.assertFalse((self.bin_dir / "thermalright_jpeg.py").exists())
                self.assertFalse(self.encoder_target.exists())
                self.assertFalse(self.encoder_manifest_target.exists())
                self.assertIn("systemctl:stop", self.event_lines())

    def test_v1_and_v2_rollbacks_refuse_a_partial_or_foreign_encoder_pair_before_stop(self) -> None:
        source = self.write_encoder_source()
        for version, names, creator in (
            ("v1", LEGACY_MODULE_NAMES, self.create_legacy_v1_backup),
            ("v2", HISTORICAL_V2_MODULE_NAMES, self.create_legacy_v2_backup),
        ):
            with self.subTest(version=version):
                paths = [*(self.bin_dir / name for name in names), self.unit_target]
                old = {path: f"{MARKER}\nold {path.name}\n" for path in paths}
                backup = creator(old, name=f"dashboard-backup-20260912T124000Z-{version}-unsafe-pair")
                installed = self.run_installer("--jpeg-encoder", str(source))
                self.assertEqual(installed.returncode, 0, installed.stderr)
                self.encoder_manifest_target.unlink()
                self.events.write_text("", encoding="utf-8")

                rejected = self.run_installer("--rollback", str(backup))

                self.assertNotEqual(rejected.returncode, 0)
                self.assertTrue(self.encoder_target.exists())
                self.assertFalse(self.encoder_manifest_target.exists())
                self.assertNotIn("systemctl:stop", self.event_lines())
                self.encoder_manifest_target.write_text(
                    f"{ENCODER_MANIFEST_VERSION}\nsha256={hashlib.sha256(source.read_bytes()).hexdigest()}\nmode=0755\n",
                    encoding="utf-8",
                )
                self.encoder_manifest_target.chmod(0o600)

    def test_v3_rollback_restores_a_prior_encoder_pair_from_the_actual_snapshot(self) -> None:
        old_source = self.write_encoder_source("old encoder", b"\x7fELFold")
        new_source = self.write_encoder_source("new encoder", b"\x7fELFnew")
        first = self.run_installer("--jpeg-encoder", str(old_source))
        self.assertEqual(first.returncode, 0, first.stderr)

        upgraded = self.run_installer("--jpeg-encoder", str(new_source))
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        backup = self.backup_dir_from(upgraded)
        self.assertIn("thermalright-dashboard-backup-v3", (backup / "manifest").read_text(encoding="utf-8"))
        self.assert_valid_encoder_pair(new_source.read_bytes())

        rolled_back = self.run_installer("--rollback", str(backup))

        self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
        self.assert_valid_encoder_pair(old_source.read_bytes())

    def test_pair_publication_failures_restore_both_prior_members_and_the_unit(self) -> None:
        old_source = self.write_encoder_source("old encoder", b"\x7fELFold")
        new_source = self.write_encoder_source("new encoder", b"\x7fELFnew")
        baseline = self.run_installer("--jpeg-encoder", str(old_source))
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        old_unit = self.unit_target.read_bytes()
        for destination in (ENCODER_NAME, ENCODER_MANIFEST_NAME, UNIT_NAME):
            with self.subTest(destination=destination):
                self.events.write_text("", encoding="utf-8")
                failed = self.run_installer(
                    "--jpeg-encoder", str(new_source), FAKE_MV_FAIL_DEST=destination,
                )
                self.assertNotEqual(failed.returncode, 0)
                self.assert_valid_encoder_pair(old_source.read_bytes())
                self.assertEqual(self.unit_target.read_bytes(), old_unit)
                self.assert_no_transaction_temporaries()

    def test_uninstall_refuses_a_partial_encoder_pair_before_disabling_the_unit(self) -> None:
        source = self.write_encoder_source()
        installed = self.run_installer("--jpeg-encoder", str(source))
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.encoder_manifest_target.unlink()
        self.events.write_text("", encoding="utf-8")

        rejected = self.run_installer("--uninstall")

        self.assertNotEqual(rejected.returncode, 0)
        self.assertTrue(self.unit_target.exists())
        self.assertTrue(self.encoder_target.exists())
        self.assertNotIn("systemctl:disable", self.event_lines())

    def test_uninstall_removes_only_valid_encoder_pair_and_preserves_unrelated_directory_files(self) -> None:
        source = self.write_encoder_source()
        installed = self.run_installer("--jpeg-encoder", str(source))
        self.assertEqual(installed.returncode, 0, installed.stderr)
        unrelated = self.encoder_dir / "keep-me"
        unrelated.write_text("unrelated\n", encoding="utf-8")
        self.events.write_text("", encoding="utf-8")

        removed = self.run_installer("--uninstall")

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse((self.bin_dir / "thermalright_jpeg.py").exists())
        self.assertFalse(self.encoder_target.exists())
        self.assertFalse(self.encoder_manifest_target.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "unrelated\n")
        self.assertIn("systemctl:disable", self.event_lines())


if __name__ == "__main__":
    unittest.main()
