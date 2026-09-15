import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from so_engine import app as so_engine


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "SO Engine.py"
APP_MODULE_PATH = PROJECT_ROOT / "so_engine" / "app.py"


def test_default_runtime_directory_stays_at_project_boundary():
    assert so_engine.default_run_directory() == PROJECT_ROOT / "runtime"


def test_installed_package_uses_current_working_directory_for_runtime(monkeypatch, tmp_path):
    installed_file = tmp_path / "site-packages" / "so_engine" / "app.py"
    working_directory = tmp_path / "work"
    working_directory.mkdir()
    monkeypatch.setattr(so_engine, "__file__", str(installed_file))
    monkeypatch.chdir(working_directory)

    assert so_engine.default_run_directory() == working_directory / "runtime"


def test_artifact_path_validator_rejects_collisions(tmp_path):
    shared_path = tmp_path / "shared.json"

    with pytest.raises(so_engine.SteamError, match="output.*checkpoint"):
        so_engine.ensure_unique_artifact_paths(
            {"output": shared_path, "checkpoint": shared_path, "audit": None}
        )


@pytest.mark.parametrize(
    ("writer_role", "sidecar_suffix"),
    [("checkpoint", ".tmp"), ("cache", ".tmp"), ("audit", ".tmp"), ("lock", ".guard")],
)
def test_artifact_path_validator_rejects_atomic_sidecar_collisions(
    tmp_path, writer_role, sidecar_suffix
):
    primary_path = tmp_path / f"{writer_role}.json"

    with pytest.raises(so_engine.SteamError, match="items_input"):
        so_engine.ensure_unique_artifact_paths(
            {
                "items_input": Path(f"{primary_path}{sidecar_suffix}"),
                writer_role: primary_path,
            }
        )


def test_cli_rejects_output_checkpoint_collision_before_processing(tmp_path):
    shared_path = tmp_path / "shared.json"
    shared_path.write_text('{"version": 999}', encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "item",
            "--output",
            str(shared_path),
            "--checkpoint",
            str(shared_path),
            "--resume",
            "--run-dir",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode != 0
    assert "output" in completed.stderr
    assert "checkpoint" in completed.stderr
    assert "совпадают" in completed.stderr


def test_cli_requires_an_explicit_input_source(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["so-engine"])
    monkeypatch.setattr(
        so_engine,
        "process_items",
        lambda *_args, **_kwargs: pytest.fail("processing must not start without explicit input"),
    )

    with pytest.raises(SystemExit) as exc_info:
        so_engine.main()

    assert exc_info.value.code == 2
    assert "--demo" in capsys.readouterr().err


def test_cli_rejects_positional_items_with_items_file(monkeypatch, capsys, tmp_path):
    items_file = tmp_path / "items.txt"
    items_file.write_text("file-item\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["so-engine", "positional-item", "--items-file", str(items_file)],
    )

    with pytest.raises(SystemExit) as exc_info:
        so_engine.main()

    assert exc_info.value.code == 2
    assert "нельзя использовать вместе" in capsys.readouterr().err


@pytest.mark.parametrize(
    "option",
    [
        "--delay",
        "--max-batch-delay",
        "--resume-max-age-minutes",
        "--proxy-quarantine-seconds",
        "--lock-stale-minutes",
    ],
)
def test_cli_rejects_non_finite_float_options(option, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["so-engine", "item", option, "nan"])
    monkeypatch.setattr(
        so_engine,
        "process_items",
        lambda *_args, **_kwargs: pytest.fail("processing must not start with NaN config"),
    )

    with pytest.raises(SystemExit) as exc_info:
        so_engine.main()

    assert exc_info.value.code == 2
    assert "конечным" in capsys.readouterr().err


def test_cli_publishes_output_while_run_lock_is_held(tmp_path, monkeypatch):
    output_path = tmp_path / "result.txt"
    lock_state = {"held": False}

    class TrackingRunLock:
        def __init__(self, _path, *, stale_minutes):
            assert stale_minutes >= 0

        def __enter__(self):
            lock_state["held"] = True
            return self

        def __exit__(self, *_args):
            lock_state["held"] = False

    original_write_text = Path.write_text

    def guarded_write_text(path, *args, **kwargs):
        if path == output_path:
            assert lock_state["held"], "output was published after the run lock was released"
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(so_engine, "RunLock", TrackingRunLock)
    monkeypatch.setattr(
        so_engine,
        "process_items",
        lambda *_args, **_kwargs: so_engine.BatchResult(
            lines=["item;1;1.23"], unresolved_count=0, skipped_count=0
        ),
    )
    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    monkeypatch.setattr(
        sys,
        "argv",
        ["so-engine", "item", "--output", str(output_path), "--run-dir", str(tmp_path)],
    )

    result = so_engine.main()

    assert result == 0
    assert output_path.read_text(encoding="utf-8") == "item;1;1.23\n"


def test_cli_returns_distinct_incomplete_exit_code_for_skipped_items(tmp_path, monkeypatch):
    monkeypatch.setattr(
        so_engine,
        "process_items",
        lambda *_args, **_kwargs: so_engine.BatchResult(
            lines=[], unresolved_count=0, skipped_count=1
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["so-engine", "item", "--run-dir", str(tmp_path), "--quiet"],
    )

    assert so_engine.main() == 3


def test_run_lock_prevents_or_preserves_replacement_owner(tmp_path):
    lock_path = tmp_path / "run.lock"
    replacement_path = tmp_path / "replacement.lock"
    lock = so_engine.RunLock(lock_path, stale_minutes=60)
    lock.acquire()
    replacement = {"pid": 999_999, "owner_token": "replacement"}
    replacement_path.write_text(json.dumps(replacement), encoding="utf-8")

    try:
        so_engine.os.replace(replacement_path, lock_path)
    except PermissionError:
        # Windows denies replacing the byte-range-locked file.
        lock.release()
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["owner_token"] == lock.owner_token
        assert payload["released"] is True
    else:
        # POSIX advisory locks allow replacement; owner-token validation preserves it.
        lock.release()
        assert json.loads(lock_path.read_text(encoding="utf-8")) == replacement


def test_run_lock_blocks_contender_after_metadata_path_replacement(tmp_path):
    lock_path = tmp_path / "run.lock"
    replacement_path = tmp_path / "replacement.lock"
    owner = so_engine.RunLock(lock_path, stale_minutes=60)
    owner.acquire()
    replacement_path.write_text(
        json.dumps({"pid": 999_999, "owner_token": "replacement"}), encoding="utf-8"
    )
    so_engine.os.replace(replacement_path, lock_path)
    script = f"""
from pathlib import Path
from so_engine import app
lock = app.RunLock(Path({str(lock_path)!r}), stale_minutes=60)
try:
    lock.acquire()
except app.SteamError:
    print('blocked')
else:
    print('acquired')
    lock.release()
"""

    contender = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    owner.release()

    assert contender.returncode == 0, contender.stderr
    assert contender.stdout.strip() == "blocked"
    assert json.loads(lock_path.read_text(encoding="utf-8"))["owner_token"] == "replacement"


def test_run_lock_guard_failure_is_reported_as_contention(tmp_path, monkeypatch):
    class FailingGuardHandle:
        def seek(self, *_args):
            return 0

        def tell(self):
            return 0

        def write(self, _payload):
            return 1

        def flush(self):
            raise PermissionError("simulated Windows guard contention")

        def close(self):
            raise PermissionError("simulated buffered close retry")

    fdopen_calls = []
    fallback_closes = []
    monkeypatch.setattr(so_engine.os, "open", lambda *_args: 123)
    monkeypatch.setattr(so_engine.os, "close", fallback_closes.append)

    def fake_fdopen(descriptor, mode, *, buffering=-1):
        fdopen_calls.append((descriptor, mode, buffering))
        return FailingGuardHandle()

    monkeypatch.setattr(so_engine.os, "fdopen", fake_fdopen)

    lock = so_engine.RunLock(tmp_path / "run.lock", stale_minutes=1)
    with pytest.raises(so_engine.SteamError, match="уже выполняется"):
        lock.acquire()

    assert fdopen_calls == [(123, "r+b", 0)]
    assert fallback_closes == [123]


def test_stale_lock_reclamation_allows_only_one_concurrent_owner(tmp_path, monkeypatch):
    lock_path = tmp_path / "run.lock"
    lock_path.write_text(json.dumps({"pid": 999_999, "owner_token": "stale"}), encoding="utf-8")
    stale_time = so_engine.time.time() - 3600
    so_engine.os.utime(lock_path, (stale_time, stale_time))

    both_unlinking = so_engine.threading.Barrier(2)
    first_acquired = so_engine.threading.Event()
    release_owners = so_engine.threading.Event()
    attempts_done = so_engine.threading.Event()
    results = []
    original_unlink = Path.unlink

    def coordinated_unlink(path, *args, **kwargs):
        if path != lock_path or release_owners.is_set():
            return original_unlink(path, *args, **kwargs)
        both_unlinking.wait(timeout=2)
        if so_engine.threading.current_thread().name == "lock-a":
            return original_unlink(path, *args, **kwargs)
        assert first_acquired.wait(timeout=2)
        return original_unlink(path, *args, **kwargs)

    def contender(name):
        lock = so_engine.RunLock(lock_path, stale_minutes=1)
        try:
            lock.acquire()
            results.append((name, "acquired"))
            if name == "a":
                first_acquired.set()
        except so_engine.SteamError:
            results.append((name, "blocked"))
        finally:
            if len(results) == 2:
                attempts_done.set()
            release_owners.wait(timeout=2)
            lock.release()

    monkeypatch.setattr(Path, "unlink", coordinated_unlink)
    threads = [
        so_engine.threading.Thread(target=contender, args=("a",), name="lock-a"),
        so_engine.threading.Thread(target=contender, args=("b",), name="lock-b"),
    ]
    for thread in threads:
        thread.start()
    assert attempts_done.wait(timeout=3)
    release_owners.set()
    for thread in threads:
        thread.join(timeout=3)

    assert sorted(status for _name, status in results) == ["acquired", "blocked"]


def test_summarize_budget_allocation_tracks_duplicate_rows_and_remainder():
    summary = so_engine.summarize_budget_allocation(
        ["A", "B", "A"],
        [3, 4, 2],
        {"A": 101, "B": 250},
        total_budget_cents=2_000,
    )

    assert summary == {
        "requested_budget_cents": 2_000,
        "actual_spend_cents": 1_505,
        "budget_remainder_cents": 495,
        "priced_output_rows": 3,
        "count_by_item": {"A": 5, "B": 4},
        "spend_by_item_cents": {"A": 505, "B": 1_000},
    }


def test_budget_allocation_never_overspends_when_mandatory_expensive_row_exceeds_equal_share():
    quantities = so_engine.allocate_equal_budget_quantities(
        ["expensive", "cheap"],
        {"expensive": 90, "cheap": 10},
        total_budget_cents=100,
    )

    assert quantities == [1, 1]


def test_budget_allocation_applies_common_floor_average_without_exceeding_budget():
    quantities = so_engine.allocate_equal_budget_quantities(
        ["cheap", "medium", "expensive"],
        {"cheap": 100, "medium": 100, "expensive": 300},
        total_budget_cents=1_200,
    )

    # The equal-budget pass produces [4, 5, 1], whose floor average is 3.
    # Three of every item would spend 1,500 cents, so the common count must
    # be reduced to the highest budget-safe value: 2. Its 200-cent remainder
    # then adds one unit to each 100-cent row after skipping the 300-cent row.
    assert quantities == [3, 3, 2]


def test_budget_allocation_spends_remainder_in_descending_price_order():
    quantities = so_engine.allocate_equal_budget_quantities(
        ["expensive", "medium", "cheap"],
        {"expensive": 300, "medium": 200, "cheap": 100},
        total_budget_cents=1_600,
    )

    # The shared floor-average baseline is [2, 2, 2], costing 1,200 cents.
    # The 400-cent remainder buys one expensive item first (300 cents), then
    # skips medium (200 cents) and buys one cheap item (100 cents).
    assert quantities == [3, 2, 3]


def test_budget_allocation_invariants_across_mixed_price_batches():
    generator = random.Random(20260715)

    for _case in range(1_000):
        row_count = generator.randint(1, 12)
        item_names = [f"item-{index}" for index in range(row_count)]
        item_prices = [generator.randint(1, 1_000) for _index in range(row_count)]
        prices = dict(zip(item_names, item_prices))
        budget_cents = sum(item_prices) + generator.randint(0, 5_000)

        quantities = so_engine.allocate_equal_budget_quantities(
            item_names,
            prices,
            total_budget_cents=budget_cents,
        )
        spend_cents = sum(
            quantity * price_cents for quantity, price_cents in zip(quantities, item_prices)
        )

        assert len(quantities) == row_count
        assert all(quantity >= 1 for quantity in quantities)
        assert spend_cents <= budget_cents


def test_budget_allocator_rejects_unbounded_subset_state(monkeypatch):
    monkeypatch.setattr(so_engine, "MAX_SUBSET_STATE_BYTES", 1)

    with pytest.raises(so_engine.SteamError, match="слишком велик"):
        so_engine.allocate_equal_budget_quantities(
            ["A", "B"], {"A": 3, "B": 5}, total_budget_cents=20
        )


def test_money_parser_accepts_russian_decimal_comma_without_changing_magnitude():
    assert so_engine.parse_nonnegative_money("20,00") == so_engine.Decimal("20.00")
    assert so_engine.parse_nonnegative_money("1,5") == so_engine.Decimal("1.5")


def test_money_parser_rejects_ambiguous_thousands_separator():
    with pytest.raises(argparse.ArgumentTypeError, match="не более двух знаков"):
        so_engine.parse_nonnegative_money("20,000")


def test_money_parser_rejects_non_finite_values():
    for value in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(argparse.ArgumentTypeError, match="конечной"):
            so_engine.parse_nonnegative_money(value)


def test_money_parser_rejects_values_above_supported_budget():
    with pytest.raises(argparse.ArgumentTypeError, match="не может превышать"):
        so_engine.parse_nonnegative_money("1E+100")


def test_self_test_checks_remain_active_under_python_optimization():
    script = """
from so_engine import app as module
module.parse_items = lambda _text: []
module.run_self_test()
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", script],
        cwd=MODULE_PATH.parent,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "SELF-TEST failed" in result.stderr


def test_json_fetch_rejects_non_steam_urls(tmp_path):
    local_payload = tmp_path / "payload.json"
    local_payload.write_text("{}", encoding="utf-8")

    with pytest.raises(so_engine.SteamError, match="steamcommunity.com"):
        so_engine.fetch(local_payload.as_uri(), timeout=1, retries=1)


@pytest.mark.parametrize(
    "url",
    [
        "https://steamcommunity.com/market/listings/730/../off-allowlist",
        "https://steamcommunity.com/market/listings/730/%2e%2e/off-allowlist",
        "https://steamcommunity.com/market/listings/730/%252e%252e%252foff-allowlist",
    ],
)
def test_steam_url_allowlist_rejects_dot_segment_normalization(url):
    with pytest.raises(so_engine.SteamError, match="Steam Market"):
        so_engine.validate_steam_url(url)


@pytest.mark.parametrize(
    "redirect_url",
    [
        "https://example.com/market/listings/730/item",
        "http://steamcommunity.com/market/listings/730/item",
    ],
)
def test_redirect_policy_rejects_cross_host_and_https_downgrade(redirect_url):
    handler = so_engine.SteamRedirectHandler()
    request = so_engine.Request("https://steamcommunity.com/market/listings/730/item")

    with pytest.raises(so_engine.SteamError, match="HTTPS URL steamcommunity.com"):
        handler.redirect_request(request, None, 302, "Found", {}, redirect_url)


class FakeSteamResponse:
    def __init__(self, payload: bytes, final_url: str):
        self.payload = payload
        self.final_url = final_url

    def geturl(self):
        return self.final_url

    def read(self, size=-1):
        return self.payload if size < 0 else self.payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FakeSteamOpener:
    def __init__(self, response):
        self.response = response

    def open(self, _request, timeout):
        return self.response


class CapturingSteamOpener(FakeSteamOpener):
    def __init__(self, response):
        super().__init__(response)
        self.request = None

    def open(self, request, timeout):
        self.request = request
        return super().open(request, timeout)


def test_fetch_uses_browser_navigation_headers_for_steam_listing(monkeypatch):
    opener = CapturingSteamOpener(
        FakeSteamResponse(b"{}", "https://steamcommunity.com/market/listings/730/item")
    )
    monkeypatch.setattr(so_engine, "build_opener", lambda *_handlers: opener)

    so_engine.fetch("https://steamcommunity.com/market/listings/730/item", retries=1)

    headers = {name.lower(): value for name, value in opener.request.header_items()}
    assert headers["accept"].startswith("text/html,application/xhtml+xml")
    assert headers["accept-encoding"] == "identity"
    assert headers["cache-control"] == "max-age=0"
    assert headers["upgrade-insecure-requests"] == "1"
    assert headers["sec-fetch-dest"] == "document"
    assert headers["sec-fetch-mode"] == "navigate"
    assert headers["sec-fetch-site"] == "none"
    assert headers["sec-fetch-user"] == "?1"


def test_fetch_uses_json_headers_for_steam_histogram(monkeypatch):
    opener = CapturingSteamOpener(
        FakeSteamResponse(b"{}", "https://steamcommunity.com/market/itemordershistogram")
    )
    monkeypatch.setattr(so_engine, "build_opener", lambda *_handlers: opener)

    so_engine.fetch(
        "https://steamcommunity.com/market/itemordershistogram?item_nameid=123",
        request_headers=so_engine.STEAM_HISTOGRAM_HEADERS,
        retries=1,
    )

    headers = {name.lower(): value for name, value in opener.request.header_items()}
    assert headers["accept"].startswith("application/json")
    assert headers["accept-encoding"] == "identity"
    assert headers["sec-fetch-dest"] == "empty"
    assert headers["sec-fetch-mode"] == "cors"
    assert headers["sec-fetch-site"] == "same-origin"
    assert "upgrade-insecure-requests" not in headers
    assert "sec-fetch-user" not in headers


def test_order_histogram_uses_json_request_profile(monkeypatch):
    captured: dict[str, object] = {}

    def fake_fetch(_url, **kwargs):
        captured.update(kwargs)
        return '{"success": 1}'

    monkeypatch.setattr(so_engine, "fetch", fake_fetch)

    assert so_engine._fetch_order_histogram("item", "123", proxy=None) == {"success": 1}
    assert captured["request_headers"] == so_engine.STEAM_HISTOGRAM_HEADERS


def test_response_reader_rejects_cross_host_final_url():
    response = FakeSteamResponse(b"{}", "https://example.com/market/itemordershistogram")

    with pytest.raises(so_engine.SteamError, match="HTTPS URL steamcommunity.com"):
        so_engine.read_steam_response(response)


def test_response_reader_enforces_body_size_limit():
    response = FakeSteamResponse(b"12345", "https://steamcommunity.com/market/itemordershistogram")

    with pytest.raises(so_engine.SteamError, match="превышает допустимый размер"):
        so_engine.read_steam_response(response, max_bytes=4)


@pytest.mark.parametrize("proxy", [None, "http://proxy.example:8080"])
def test_fetch_validates_final_url_for_direct_and_proxy_requests(monkeypatch, proxy):
    response = FakeSteamResponse(b"{}", "https://example.com/market/itemordershistogram")
    monkeypatch.setattr(so_engine, "build_opener", lambda *_handlers: FakeSteamOpener(response))

    with pytest.raises(so_engine.SteamError, match="HTTPS URL steamcommunity.com"):
        so_engine.fetch(
            "https://steamcommunity.com/market/itemordershistogram",
            proxy=proxy,
            retries=1,
        )


def test_normalize_proxy_accepts_authenticated_socks5_url():
    proxy = "socks5://user:password@proxy.example:1080"

    assert so_engine.normalize_proxy(proxy) == proxy


def test_normalize_proxy_expands_vendor_format_to_https():
    assert (
        so_engine.normalize_proxy("proxy.example:8443:user:password")
        == "https://user:password@proxy.example:8443"
    )


def test_redact_proxy_never_discloses_proxy_credentials_or_address():
    proxy = "http://user:password@proxy.example:8080"

    assert so_engine.redact_proxy(proxy) == "configured-proxy"


def test_process_items_retries_http_429_with_next_proxy(tmp_path, monkeypatch):
    attempted_proxies: list[str | None] = []

    def fake_snapshot(_item_name, *, proxy, **_kwargs):
        attempted_proxies.append(proxy)
        if len(attempted_proxies) == 1:
            raise so_engine.SteamHTTPError(429)
        return so_engine.MarketSnapshot(
            decision=so_engine.BidDecision(
                top_bid_cents=120,
                band_lo_cents=104,
                band_hi_cents=110,
                price_cents=105,
                mode="band_bottom",
                reason="test",
                queue_ahead=1,
                discount_bps=1_250,
                walls=(),
                warnings=(),
            ),
            best_sell_cents=130,
            visible_buy_orders=10,
            visible_sell_orders=10,
        )

    monkeypatch.setattr(so_engine, "get_market_snapshot", fake_snapshot)
    result = so_engine.process_items(
        ["rate-limited-item"],
        proxies=["http://first.proxy:80", "http://second.proxy:80"],
        delay=0,
        request_delay_ms=0,
        debug=False,
        cache_path=tmp_path / "cache.json",
    )

    assert result.is_complete is True
    assert result.lines == ["rate-limited-item;1;1.05"]
    assert attempted_proxies == ["http://first.proxy:80", "http://second.proxy:80"]


def test_process_items_records_common_average_remainder_allocation_in_audit(tmp_path, monkeypatch):
    prices = {"A": 101, "B": 250}

    def fake_snapshot(item_name, **_kwargs):
        price_cents = prices[item_name]
        return so_engine.MarketSnapshot(
            decision=so_engine.BidDecision(
                top_bid_cents=300,
                band_lo_cents=261,
                band_hi_cents=276,
                price_cents=price_cents,
                mode="above_wall",
                reason="test",
                queue_ahead=5,
                discount_bps=1_000,
                walls=(),
                warnings=(),
            ),
            best_sell_cents=price_cents + 100,
            visible_buy_orders=10,
            visible_sell_orders=10,
        )

    monkeypatch.setattr(so_engine, "get_market_snapshot", fake_snapshot)
    audit_path = tmp_path / "audit.json"
    lines, errors = so_engine.process_items(
        ["A", "B", "A"],
        proxies=[],
        delay=0,
        request_delay_ms=0,
        debug=False,
        total_budget_cents=2_000,
        audit_path=audit_path,
        cache_path=tmp_path / "cache.json",
    )

    assert errors == 0
    assert lines == ["A;5;1.01", "B;4;2.50", "A;4;1.01"]
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["metadata"]["requested_budget_cents"] == "2000"
    assert audit["metadata"]["actual_spend_cents"] == "1909"
    assert audit["metadata"]["budget_remainder_cents"] == "91"
    records = {record["item"]: record for record in audit["items"]}
    assert records["A"]["allocation_count"] == 9
    assert records["A"]["allocation_spend_cents"] == 909
    assert records["B"]["allocation_count"] == 4
    assert records["B"]["allocation_spend_cents"] == 1000


def test_process_items_reports_filter_skips_as_incomplete(tmp_path, monkeypatch):
    def fake_snapshot(_item_name, **_kwargs):
        return so_engine.MarketSnapshot(
            decision=so_engine.BidDecision(
                top_bid_cents=120,
                band_lo_cents=104,
                band_hi_cents=110,
                price_cents=105,
                mode="band_bottom",
                reason="test",
                queue_ahead=1,
                discount_bps=1_250,
                walls=(),
                warnings=(),
            ),
            best_sell_cents=130,
            visible_buy_orders=1,
            visible_sell_orders=1,
        )

    monkeypatch.setattr(so_engine, "get_market_snapshot", fake_snapshot)

    result = so_engine.process_items(
        ["filtered-item"],
        proxies=[],
        delay=0,
        request_delay_ms=0,
        debug=False,
        market_filter=so_engine.MarketFilterConfig(min_visible_buy_orders=2),
        cache_path=tmp_path / "cache.json",
    )

    assert result.lines == []
    assert result.unresolved_count == 0
    assert result.skipped_count == 1
    assert result.is_complete is False


def test_resume_rejects_unknown_checkpoint_schema(tmp_path):
    checkpoint_path = tmp_path / "progress.json"
    checkpoint_path.write_text(
        json.dumps(
            {
                "version": 999,
                "metadata": {},
                "prices": {"item": 123},
                "price_timestamps": {
                    "item": so_engine.datetime.now(so_engine.timezone.utc).isoformat()
                },
                "failures": {},
                "failure_timestamps": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(so_engine.SteamError, match="версия checkpoint"):
        so_engine.load_progress_state(checkpoint_path)


@pytest.mark.parametrize("payload", ["{broken", "[]", "null"])
def test_resume_rejects_malformed_or_non_object_checkpoint(tmp_path, payload):
    checkpoint_path = tmp_path / "progress.json"
    checkpoint_path.write_text(payload, encoding="utf-8")

    with pytest.raises(so_engine.SteamError, match="checkpoint"):
        so_engine.load_progress_state(checkpoint_path)


def test_resume_rejects_incomplete_checkpoint_metadata(tmp_path):
    checkpoint_path = tmp_path / "progress.json"
    checkpoint_path.write_text(
        json.dumps(
            {
                "version": so_engine.CHECKPOINT_SCHEMA_VERSION,
                "metadata": {},
                "prices": {"item": 123},
                "price_timestamps": {
                    "item": so_engine.datetime.now(so_engine.timezone.utc).isoformat()
                },
                "failures": {},
                "failure_timestamps": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(so_engine.SteamError, match="metadata checkpoint"):
        so_engine.load_progress_state(checkpoint_path)


def test_checkpoint_writer_rejects_incomplete_metadata(tmp_path):
    checkpoint_path = tmp_path / "progress.json"

    with pytest.raises(so_engine.SteamError, match="metadata checkpoint"):
        so_engine.save_progress(checkpoint_path, {"item": 123}, {}, metadata={})

    assert not checkpoint_path.exists()


def test_resume_force_drops_checkpoint_items_not_present_in_current_input(tmp_path, monkeypatch):
    checkpoint_path = tmp_path / "progress.json"
    timestamp = so_engine.datetime.now(so_engine.timezone.utc).isoformat()
    so_engine.save_progress(
        checkpoint_path,
        {"old-item": 111},
        {},
        metadata={
            "algorithm_version": so_engine.ALGORITHM_VERSION,
            "input_sha256": "different-input",
            "strategy_sha256": "different-strategy",
        },
        price_timestamps={"old-item": timestamp},
    )

    def fake_snapshot(_item_name, **_kwargs):
        return so_engine.MarketSnapshot(
            decision=so_engine.BidDecision(
                top_bid_cents=120,
                band_lo_cents=104,
                band_hi_cents=110,
                price_cents=105,
                mode="band_bottom",
                reason="test",
                queue_ahead=1,
                discount_bps=1_250,
                walls=(),
                warnings=(),
            ),
            best_sell_cents=130,
            visible_buy_orders=10,
            visible_sell_orders=10,
        )

    monkeypatch.setattr(so_engine, "get_market_snapshot", fake_snapshot)
    audit_path = tmp_path / "audit.json"
    lines, errors = so_engine.process_items(
        ["new-item"],
        proxies=[],
        delay=0,
        request_delay_ms=0,
        debug=False,
        checkpoint_path=checkpoint_path,
        resume=True,
        resume_force=True,
        total_budget_cents=1_000,
        audit_path=audit_path,
        cache_path=tmp_path / "cache.json",
    )

    assert errors == 0
    assert lines == ["new-item;9;1.05"]
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["metadata"]["successful_unique"] == "1"
    state = so_engine.load_progress_state(checkpoint_path)
    assert state.prices == {"new-item": 105}


def test_resume_force_rewrites_filtered_checkpoint_when_every_item_is_reused(tmp_path, monkeypatch):
    checkpoint_path = tmp_path / "progress.json"
    timestamp = so_engine.datetime.now(so_engine.timezone.utc).isoformat()
    so_engine.save_progress(
        checkpoint_path,
        {"current-item": 123, "unrelated-item": 456},
        {},
        metadata={
            "algorithm_version": so_engine.ALGORITHM_VERSION,
            "input_sha256": "stale-input",
            "strategy_sha256": "stale-strategy",
        },
        price_timestamps={"current-item": timestamp, "unrelated-item": timestamp},
    )
    monkeypatch.setattr(
        so_engine,
        "get_market_snapshot",
        lambda *_args, **_kwargs: pytest.fail("fresh checkpoint item must be reused"),
    )

    lines, errors = so_engine.process_items(
        ["current-item"],
        proxies=[],
        delay=0,
        request_delay_ms=0,
        debug=False,
        checkpoint_path=checkpoint_path,
        resume=True,
        resume_force=True,
        cache_path=tmp_path / "cache.json",
    )

    assert errors == 0
    assert lines == ["current-item;1;1.23"]
    state = so_engine.load_progress_state(checkpoint_path)
    assert state.prices == {"current-item": 123}
    assert state.metadata["input_sha256"] == so_engine.input_fingerprint(["current-item"])
    assert state.metadata["strategy_sha256"] != "stale-strategy"


def test_describe_contract_is_network_free_machine_readable_cli():
    completed = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--describe-contract"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload == {
        "app_version": "3.3.0",
        "algorithm_version": "fifo-wall-aware-v4",
        "audit_schema_version": 1,
        "checkpoint_schema_version": 2,
        "output_format": "Item;count;price",
        "pricing_source": "so_engine.selector:choose_bid_order",
    }
