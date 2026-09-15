import json
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_compatibility_launchers_stay_thin_and_delegate_to_package():
    app_launcher = (PROJECT_ROOT / "SO Engine.py").read_text(encoding="utf-8")
    selector_launcher = (PROJECT_ROOT / "bid_order_algorithm.py").read_text(encoding="utf-8")

    assert len([line for line in app_launcher.splitlines() if line.strip()]) <= 10
    assert "from so_engine.app import main" in app_launcher
    assert len([line for line in selector_launcher.splitlines() if line.strip()]) <= 20
    assert "from so_engine.selector import" in selector_launcher


def test_windows_launcher_passes_the_persistent_proxy_pool():
    launcher = (PROJECT_ROOT / "launcher" / "start.cmd").read_text(encoding="utf-8")
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert '--proxy-file "%CD%\\proxies.txt"' in launcher
    assert "proxies*.txt" in gitignore


def test_editable_install_uses_ascii_bootstrap_for_unicode_project_paths():
    configuration = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert configuration["tool"]["hatch"]["build"]["dev-mode-exact"] is True
    assert any(
        dependency.startswith("editables")
        for dependency in configuration["dependency-groups"]["dev"]
    )


def test_package_module_delegates_to_production_cli():
    completed = subprocess.run(
        [sys.executable, "-m", "so_engine", "--describe-contract"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["algorithm_version"] == "fifo-wall-aware-v4"
    assert payload["pricing_source"] == "so_engine.selector:choose_bid_order"


def test_package_cli_reports_application_version_without_network():
    completed = subprocess.run(
        [sys.executable, "-m", "so_engine", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "SO Engine 3.3.0"


def test_cli_help_reports_fixed_nine_to_thirteen_percent_policy():
    completed = subprocess.run(
        [sys.executable, "-m", "so_engine", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "постоянный" in completed.stdout
    assert "диапазон 9–13%" in completed.stdout
    assert "--min-discount-bps" not in completed.stdout
    assert "--max-discount-bps" not in completed.stdout


def test_cli_rejects_custom_discount_band():
    completed = subprocess.run(
        [sys.executable, "-m", "so_engine", "--demo", "--min-discount-bps", "1200"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "unrecognized arguments" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_cli_reports_missing_items_file_without_traceback(tmp_path):
    missing = tmp_path / "missing-items.txt"

    completed = subprocess.run(
        [sys.executable, "-m", "so_engine", "--items-file", str(missing)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "не удалось прочитать --items-file" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_cli_rejects_items_file_without_rows(tmp_path):
    items_file = tmp_path / "empty-items.txt"
    items_file.write_text("# only a comment\n\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "so_engine", "--items-file", str(items_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "не содержит предметов" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_cli_rejects_malformed_item_row_without_traceback(tmp_path):
    items_file = tmp_path / "bad-items.txt"
    items_file.write_text("Bad Item;not-an-integer;1.00\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "so_engine", "--items-file", str(items_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "Traceback" not in completed.stderr


def test_cli_rejects_non_utf8_items_file_without_traceback(tmp_path):
    items_file = tmp_path / "bad-encoding-items.txt"
    items_file.write_bytes(b"Item;1;1.00\xff")

    completed = subprocess.run(
        [sys.executable, "-m", "so_engine", "--items-file", str(items_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "Traceback" not in completed.stderr


def test_cli_reports_missing_proxy_file_without_traceback(tmp_path):
    missing = tmp_path / "missing-proxies.txt"

    completed = subprocess.run(
        [sys.executable, "-m", "so_engine", "item", "--proxy-file", str(missing)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "не удалось прочитать --proxy-file" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_cli_rejects_non_utf8_proxy_file_without_traceback(tmp_path):
    proxy_file = tmp_path / "bad-encoding-proxies.txt"
    proxy_file.write_bytes(b"host:80\xff")

    completed = subprocess.run(
        [sys.executable, "-m", "so_engine", "item", "--proxy-file", str(proxy_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "Traceback" not in completed.stderr


def test_cli_reports_invalid_inline_proxy_without_traceback():
    completed = subprocess.run(
        [sys.executable, "-m", "so_engine", "item", "--proxy", "not-a-proxy"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "некоррект" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_cli_reports_checkpoint_error_without_traceback(tmp_path):
    checkpoint = tmp_path / "progress.json"
    checkpoint.write_text("{broken", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "so_engine",
            "item",
            "--run-dir",
            str(tmp_path),
            "--checkpoint",
            str(checkpoint),
            "--resume",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "checkpoint" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_cli_reports_run_directory_error_without_traceback(tmp_path):
    run_dir = tmp_path / "not-a-directory"
    run_dir.write_text("occupied", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "so_engine",
            "item",
            "--run-dir",
            str(run_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "ERROR:" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_wheel_contains_compatibility_adapter_and_typing_marker(tmp_path):
    completed = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    wheel_path = next(tmp_path.glob("so_engine-*.whl"))
    with zipfile.ZipFile(wheel_path) as wheel:
        members = set(wheel.namelist())

    assert "bid_order_algorithm.py" in members
    assert "so_engine/py.typed" in members
