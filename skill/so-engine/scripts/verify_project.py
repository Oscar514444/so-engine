#!/usr/bin/env python
"""Network-free release gate for the program/skill boundary."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import sysconfig
import tempfile
import time
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SKILL_PATH = PROJECT_ROOT / "skill" / "so-engine" / "SKILL.md"
EXPECTED_CONTRACT = {
    "app_version": "3.3.0",
    "algorithm_version": "fifo-wall-aware-v4",
    "audit_schema_version": 1,
    "checkpoint_schema_version": 2,
    "output_format": "Item;count;price",
    "pricing_source": "so_engine.selector:choose_bid_order",
}


def run(command: list[str], *, cwd: Path = PROJECT_ROOT, reject_stderr: bool = False) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        details = (completed.stdout + completed.stderr).strip()
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n{details}"
        )
    if reject_stderr and completed.stderr.strip():
        raise RuntimeError(
            f"command wrote unexpected stderr: {' '.join(command)}\n{completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def verify_contract() -> None:
    payload = json.loads(run([sys.executable, "-m", "so_engine", "--describe-contract"]))
    if payload != EXPECTED_CONTRACT:
        raise RuntimeError(f"contract mismatch: expected {EXPECTED_CONTRACT!r}, got {payload!r}")

    skill_text = SKILL_PATH.read_text(encoding="utf-8")
    version_match = re.search(r"(?m)^version:\s*([^\s]+)\s*$", skill_text)
    if version_match is None or version_match.group(1) != payload["app_version"]:
        raise RuntimeError("project skill version does not match app_version")
    if payload["pricing_source"] not in skill_text:
        raise RuntimeError("project skill does not point to the canonical pricing source")
    if "target_buy_price =" in skill_text:
        raise RuntimeError("project skill contains a duplicate pricing implementation")

    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = pyproject.get("project", {}).get("version")
    if project_version != payload["app_version"]:
        raise RuntimeError("pyproject version does not match app_version")

    package_version = run([sys.executable, "-c", "import so_engine; print(so_engine.__version__)"])
    if package_version != payload["app_version"]:
        raise RuntimeError("package __version__ does not match app_version")


def verify_quick() -> None:
    verify_contract()
    output = run([sys.executable, "-m", "so_engine", "--self-test"])
    if output != "SELF-TEST: OK":
        raise RuntimeError(f"unexpected self-test output: {output!r}")
    verify_editable_install()


def verify_editable_install() -> None:
    """Smoke the editable module and console script from outside the checkout."""

    executable_name = "so-engine.exe" if sys.platform == "win32" else "so-engine"
    console_script = Path(sys.executable).with_name(executable_name)
    if not console_script.is_file():
        raise RuntimeError(f"editable console script not found: {console_script}")

    verify_ascii_editable_bootstrap(Path(sysconfig.get_path("purelib")))

    with tempfile.TemporaryDirectory(prefix="so-engine-editable-smoke-") as temp_dir:
        outside_checkout = Path(temp_dir)
        self_test = run(
            [sys.executable, "-m", "so_engine", "--self-test"],
            cwd=outside_checkout,
            reject_stderr=True,
        )
        if self_test != "SELF-TEST: OK":
            raise RuntimeError(f"unexpected editable self-test output: {self_test!r}")
        contract = json.loads(
            run(
                [str(console_script), "--describe-contract"],
                cwd=outside_checkout,
                reject_stderr=True,
            )
        )

    expected = EXPECTED_CONTRACT
    if contract != expected:
        raise RuntimeError(
            f"editable console contract mismatch: expected {expected!r}, got {contract!r}"
        )


def verify_ascii_editable_bootstrap(site_packages: Path) -> None:
    """Require Hatchling's ASCII-only exact-editable .pth bootstrap."""

    candidates = list(site_packages.glob("*_so_engine.pth"))
    if len(candidates) != 1:
        raise RuntimeError(f"expected one SO Engine editable .pth, found: {candidates!r}")
    try:
        bootstrap = candidates[0].read_bytes().decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("editable .pth is not an ASCII bootstrap") from exc
    if bootstrap != "import _editable_impl_so_engine":
        raise RuntimeError(f"editable .pth is not the expected ASCII bootstrap: {bootstrap!r}")


def release_windows_executable(path: Path, *, attempts: int = 20) -> None:
    """Remove a console wrapper after transient Windows/AV handle retention."""

    if sys.platform != "win32":
        return
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.05)


def verify_wheel() -> None:
    """Build, install, and smoke-test the release artifact outside the source tree."""
    with tempfile.TemporaryDirectory(prefix="so-engine-wheel-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        dist_dir = temporary_root / "dist"
        environment_dir = temporary_root / "venv"
        run(["uv", "build", "--out-dir", str(dist_dir)])
        wheels = list(dist_dir.glob("so_engine-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one wheel, found: {wheels!r}")
        sdists = list(dist_dir.glob("so_engine-*.tar.gz"))
        if len(sdists) != 1:
            raise RuntimeError(f"expected one sdist, found: {sdists!r}")

        run(["uv", "venv", str(environment_dir)])
        environment_python = (
            environment_dir / "Scripts" / "python.exe"
            if sys.platform == "win32"
            else environment_dir / "bin" / "python"
        )
        executable_name = "so-engine.exe" if sys.platform == "win32" else "so-engine"
        isolated_console = environment_python.with_name(executable_name)
        run(["uv", "pip", "install", "--python", str(environment_python), str(wheels[0])])

        isolated_python = [str(environment_python), "-I"]
        version = run([*isolated_python, "-m", "so_engine", "--version"])
        if version != f"SO Engine {EXPECTED_CONTRACT['app_version']}":
            raise RuntimeError(f"unexpected installed version output: {version!r}")
        installed_contract = json.loads(
            run([*isolated_python, "-m", "so_engine", "--describe-contract"])
        )
        if installed_contract != EXPECTED_CONTRACT:
            raise RuntimeError("installed wheel contract mismatch")
        try:
            console_output = run([str(isolated_console), "--describe-contract"])
        finally:
            release_windows_executable(isolated_console)
        console_contract = json.loads(console_output)
        if console_contract != EXPECTED_CONTRACT:
            raise RuntimeError("installed console contract mismatch")
        if run([*isolated_python, "-m", "so_engine", "--self-test"]) != "SELF-TEST: OK":
            raise RuntimeError("installed wheel self-test failed")
        adapter_source = run(
            [
                *isolated_python,
                "-c",
                "import bid_order_algorithm as b; print(b.choose_bid_order.__module__)",
            ]
        )
        if adapter_source != "so_engine.selector":
            raise RuntimeError("installed compatibility adapter does not delegate to selector")
        typing_marker = run(
            [
                *isolated_python,
                "-c",
                "from importlib.resources import files; "
                "print(files('so_engine').joinpath('py.typed').is_file())",
            ]
        )
        if typing_marker != "True":
            raise RuntimeError("installed wheel is missing so_engine/py.typed")


def verify_full() -> None:
    verify_quick()
    commands = [
        [
            sys.executable,
            "-m",
            "py_compile",
            "SO Engine.py",
            "bid_order_algorithm.py",
            "so_engine/app.py",
            "so_engine/selector.py",
        ],
        [sys.executable, "-O", "-m", "so_engine", "--self-test"],
        [sys.executable, "-m", "pytest", "-q"],
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "mypy"],
        [sys.executable, "-m", "bandit", "-q", "-r", "so_engine"],
        [sys.executable, "-m", "coverage", "erase"],
        [sys.executable, "-m", "coverage", "run", "-m", "pytest", "-q"],
        [sys.executable, "-m", "coverage", "report", "--fail-under=42"],
    ]
    for command in commands:
        run(command)
    verify_wheel()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="Run contract and self-test only")
    args = parser.parse_args()
    try:
        verify_quick() if args.quick else verify_full()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"SO ENGINE VERIFICATION: FAIL\n{exc}", file=sys.stderr)
        return 1
    print("SO ENGINE VERIFICATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
