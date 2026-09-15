import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_SKILL = PROJECT_ROOT / "skill" / "so-engine" / "SKILL.md"


def test_project_skill_is_a_thin_adapter_to_the_program():
    text = PROJECT_SKILL.read_text(encoding="utf-8")
    substantive_lines = [line for line in text.splitlines() if line.strip()]

    assert len(substantive_lines) <= 110
    assert "so-engine --describe-contract" in text
    assert "python -m so_engine" in text
    assert "Never modify the price-calculation algorithm" in text
    assert "source of truth" in text.lower()
    assert "target_buy_price =" not in text

    references = PROJECT_SKILL.parent / "references"
    assert {path.name for path in references.glob("*.md")} >= {
        "adversarial-release-review.md",
        "architecture-and-operations.md",
        "financial-safety-and-verification.md",
        "maintenance-and-optimization.md",
        "windows-unicode-editable-installs.md",
    }

    project_python_files = [
        *PROJECT_ROOT.glob("*.py"),
        *(PROJECT_ROOT / "so_engine").rglob("*.py"),
        *(PROJECT_ROOT / "skill").rglob("*.py"),
    ]
    selector_implementations = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in project_python_files
        if re.search(r"(?m)^def choose_bid_order\(", path.read_text(encoding="utf-8"))
    ]
    assert selector_implementations == ["so_engine/selector.py"]


def test_project_verifier_checks_program_skill_contract_without_network():
    verifier = PROJECT_SKILL.parent / "scripts" / "verify_project.py"
    completed = subprocess.run(
        [sys.executable, str(verifier), "--quick"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "SO ENGINE VERIFICATION: PASS" in completed.stdout


def test_project_verifier_smokes_an_editable_install_outside_checkout():
    verifier_path = PROJECT_SKILL.parent / "scripts" / "verify_project.py"
    spec = importlib.util.spec_from_file_location(
        "so_engine_project_verifier_editable", verifier_path
    )
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)

    verifier.verify_editable_install()


def test_project_verifier_rejects_editable_startup_warnings(monkeypatch):
    verifier_path = PROJECT_SKILL.parent / "scripts" / "verify_project.py"
    spec = importlib.util.spec_from_file_location(
        "so_engine_project_verifier_stderr", verifier_path
    )
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)

    completed = subprocess.CompletedProcess([], 0, "SELF-TEST: OK\n", "pth startup warning\n")
    monkeypatch.setattr(verifier.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(RuntimeError, match="stderr"):
        verifier.run(["so-engine", "--self-test"], reject_stderr=True)


def test_project_verifier_rejects_non_ascii_editable_bootstrap(tmp_path):
    verifier_path = PROJECT_SKILL.parent / "scripts" / "verify_project.py"
    spec = importlib.util.spec_from_file_location("so_engine_project_verifier_pth", verifier_path)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)

    (tmp_path / "_editable_impl_so_engine.pth").write_text(
        "C:/checkout/Скрипт для стима\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="ASCII bootstrap"):
        verifier.verify_ascii_editable_bootstrap(tmp_path)


def test_project_verifier_retries_windows_console_cleanup(tmp_path, monkeypatch):
    verifier_path = PROJECT_SKILL.parent / "scripts" / "verify_project.py"
    spec = importlib.util.spec_from_file_location(
        "so_engine_project_verifier_cleanup", verifier_path
    )
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)

    executable = tmp_path / "so-engine.exe"
    executable.touch()
    unlink_attempts = 0
    original_unlink = Path.unlink

    def flaky_unlink(path, *args, **kwargs):
        nonlocal unlink_attempts
        unlink_attempts += 1
        if unlink_attempts < 3:
            raise PermissionError("simulated antivirus hold")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(verifier.sys, "platform", "win32")
    monkeypatch.setattr(verifier.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    verifier.release_windows_executable(executable)

    assert unlink_attempts == 3
    assert not executable.exists()


def test_project_verifier_smokes_an_isolated_wheel_installation():
    verifier_path = PROJECT_SKILL.parent / "scripts" / "verify_project.py"
    spec = importlib.util.spec_from_file_location("so_engine_project_verifier", verifier_path)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)

    verifier.verify_wheel()


def test_project_verifier_builds_sdist_and_smokes_console_entry_point():
    verifier_source = (PROJECT_SKILL.parent / "scripts" / "verify_project.py").read_text(
        encoding="utf-8"
    )

    assert 'dist_dir.glob("so_engine-*.tar.gz")' in verifier_source
    assert "isolated_console" in verifier_source
    assert 'run([str(isolated_console), "--describe-contract"]' in verifier_source


def test_project_files_are_grouped_by_purpose():
    assert not list(PROJECT_ROOT.glob("test_*.py"))
    assert {
        "test_bid_order_algorithm.py",
        "test_package_cli.py",
        "test_skill_contract.py",
        "test_so_engine.py",
    } <= {path.name for path in (PROJECT_ROOT / "tests").glob("test_*.py")}
    assert (PROJECT_ROOT / "docs" / "CHANGELOG.md").is_file()
    assert not (PROJECT_ROOT / "CHANGELOG.md").exists()
    assert (PROJECT_ROOT / "archive" / "batch_refresh").is_dir()
    assert not (PROJECT_ROOT / "batch_refresh").exists()
    assert (PROJECT_ROOT / "launcher" / "start.cmd").is_file()
    assert not (PROJECT_ROOT / ".launcher").exists()
