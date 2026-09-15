#!/usr/bin/env python
"""Steam Order Engine: fixed-band CS2 Steam buy-order calculator.

The script fetches Steam's live cumulative ``buy_order_graph`` and selects a
buy-order price inside the permanent 9–13% discount band and applies
structural-wall rules. All internal prices are integer cents.

Output:
    Item Name;count;price
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import re
import socks
import ssl
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from html import unescape
from http.client import HTTPSConnection
from pathlib import Path
from types import TracebackType
from typing import Any, Iterator, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .selector import (
    BidDecision,
    BidLevel,
    BidOrderConfig,
    choose_bid_order,
    decompose_cumulative_buy_orders,
)

APP_ID = 730
LEGACY_MARKET_COOKIE = "bMarketOptOut=1"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
STEAM_NAVIGATION_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Encoding": "identity",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
STEAM_HISTOGRAM_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Encoding": "identity",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Requested-With": "XMLHttpRequest",
}

DEFAULT_ITEMS = [
    "P2000 | Imperial (Factory New)",
    "XM1014 | Charter (Field-Tested)",
    "StatTrak™ Glock-18 | Off World (Well-Worn)",
    "Desert Eagle | Light Rail (Battle-Scarred)",
]
APP_VERSION = "3.3.0"
ALGORITHM_VERSION = "fifo-wall-aware-v4"
AUDIT_SCHEMA_VERSION = 1
CHECKPOINT_SCHEMA_VERSION = 2
CHECKPOINT_METADATA_KEYS = frozenset({"algorithm_version", "input_sha256", "strategy_sha256"})


def contract_payload() -> dict[str, str | int]:
    """Return the stable machine-readable boundary used by CLI clients and skills."""

    return {
        "app_version": APP_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "output_format": "Item;count;price",
        "pricing_source": "so_engine.selector:choose_bid_order",
    }


@dataclass(frozen=True)
class MarketSnapshot:
    """Visible order-book facts required to judge a buy recommendation."""

    decision: BidDecision
    best_sell_cents: int | None
    visible_buy_orders: int
    visible_sell_orders: int


@dataclass(frozen=True)
class MarketFilterConfig:
    """Optional safety filters; zero thresholds preserve legacy behaviour."""

    min_spread_bps: int = 0
    min_net_margin_bps: int = 0
    sell_fee_bps: int = 0
    max_queue_ahead: int = 0
    min_visible_buy_orders: int = 0

    def __post_init__(self) -> None:
        values = (
            self.min_spread_bps,
            self.min_net_margin_bps,
            self.sell_fee_bps,
            self.max_queue_ahead,
            self.min_visible_buy_orders,
        )
        if any(value < 0 for value in values):
            raise ValueError("market filter values cannot be negative")
        if self.sell_fee_bps >= 10_000:
            raise ValueError("sell_fee_bps must be below 10000")


@dataclass(frozen=True)
class MarketRecommendation:
    status: str
    reason: str
    recommended: bool
    gross_spread_bps: int | None
    net_margin_bps: int | None


@dataclass(frozen=True)
class BatchResult:
    """Structured completion state for one batch.

    Iteration preserves the historical ``lines, unresolved = process_items(...)``
    adapter while callers migrate to the explicit completion fields.
    """

    lines: list[str]
    unresolved_count: int
    skipped_count: int

    @property
    def is_complete(self) -> bool:
        return self.unresolved_count == 0 and self.skipped_count == 0

    def __iter__(self) -> Iterator[list[str] | int]:
        yield self.lines
        yield self.unresolved_count


def evaluate_market(snapshot: MarketSnapshot, config: MarketFilterConfig) -> MarketRecommendation:
    """Apply transparent liquidity, queue, spread, and fee-aware margin gates."""
    decision = snapshot.decision
    if snapshot.best_sell_cents is None or snapshot.best_sell_cents <= 0:
        return MarketRecommendation(
            "SKIP_NO_SELL", "Нет видимого sell order для оценки выхода.", False, None, None
        )

    gross_spread_bps = (
        (snapshot.best_sell_cents - decision.price_cents) * 10_000 // decision.price_cents
    )
    net_sell_cents = snapshot.best_sell_cents * (10_000 - config.sell_fee_bps) // 10_000
    net_margin_bps = (net_sell_cents - decision.price_cents) * 10_000 // decision.price_cents
    if (
        config.min_visible_buy_orders
        and snapshot.visible_buy_orders < config.min_visible_buy_orders
    ):
        return MarketRecommendation(
            "SKIP_LIQUIDITY",
            f"Видимых buy orders: {snapshot.visible_buy_orders}, минимум: {config.min_visible_buy_orders}.",
            False,
            gross_spread_bps,
            net_margin_bps,
        )
    if config.max_queue_ahead and decision.queue_ahead > config.max_queue_ahead:
        return MarketRecommendation(
            "SKIP_QUEUE",
            f"Видимая очередь {decision.queue_ahead} превышает лимит {config.max_queue_ahead}.",
            False,
            gross_spread_bps,
            net_margin_bps,
        )
    if gross_spread_bps < config.min_spread_bps:
        return MarketRecommendation(
            "SKIP_SPREAD",
            f"Спред {gross_spread_bps} bps ниже минимума {config.min_spread_bps} bps.",
            False,
            gross_spread_bps,
            net_margin_bps,
        )
    if net_margin_bps < config.min_net_margin_bps:
        return MarketRecommendation(
            "SKIP_MARGIN",
            f"Маржа после комиссии {net_margin_bps} bps ниже минимума {config.min_net_margin_bps} bps.",
            False,
            gross_spread_bps,
            net_margin_bps,
        )
    return MarketRecommendation(
        "BUY", "Прошел все включенные фильтры.", True, gross_spread_bps, net_margin_bps
    )


@dataclass
class ProgressState:
    prices: dict[str, int]
    failures: dict[str, str]
    price_timestamps: dict[str, str]
    failure_timestamps: dict[str, str]
    metadata: dict[str, str]

    def reusable_prices(
        self, *, max_age_minutes: float, now: datetime | None = None
    ) -> dict[str, int]:
        if max_age_minutes <= 0:
            return dict(self.prices)
        now = now or datetime.now(timezone.utc)
        reusable: dict[str, int] = {}
        for item_name, price_cents in self.prices.items():
            try:
                calculated_at = datetime.fromisoformat(
                    self.price_timestamps[item_name].replace("Z", "+00:00")
                )
            except (KeyError, TypeError, ValueError):
                continue
            if calculated_at.tzinfo is None:
                calculated_at = calculated_at.replace(tzinfo=timezone.utc)
            age_minutes = (now - calculated_at.astimezone(timezone.utc)).total_seconds() / 60
            if 0 <= age_minutes <= max_age_minutes:
                reusable[item_name] = price_cents
        return reusable


class SteamError(RuntimeError):
    pass


MAX_STEAM_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BUDGET = Decimal("100000.00")
MAX_SUBSET_STATE_BYTES = 256 * 1024 * 1024


def _is_allowed_steam_path(path: str) -> bool:
    if path == "/market/itemordershistogram":
        return True
    listing_prefix = f"/market/listings/{APP_ID}/"
    if not path.startswith(listing_prefix):
        return False
    encoded_name = path[len(listing_prefix) :]
    if not encoded_name or "/" in encoded_name or "\\" in encoded_name:
        return False
    decoded_name = encoded_name
    for _pass in range(8):
        next_name = unquote(decoded_name)
        if next_name == decoded_name:
            break
        decoded_name = next_name
    else:
        return False
    return decoded_name not in {".", ".."} and "/" not in decoded_name and "\\" not in decoded_name


def validate_steam_url(url: str) -> None:
    """Reject redirects and direct requests outside the exact Steam Market allowlist."""
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SteamError("разрешены только HTTPS URL steamcommunity.com") from exc
    allowed_path = _is_allowed_steam_path(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "steamcommunity.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not allowed_path
    ):
        raise SteamError("разрешены только HTTPS URL steamcommunity.com для Steam Market")


class SteamRedirectHandler(HTTPRedirectHandler):
    """Allow urllib redirects only when the destination remains in the Steam allowlist."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        validate_steam_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Socks5HTTPSConnection(HTTPSConnection):
    """HTTPS connection whose TCP socket is established through one SOCKS5 proxy."""

    def __init__(self, host: str, *, proxy_url: str, **kwargs: Any) -> None:
        source_address = kwargs.get("source_address")
        context = kwargs.get("context")
        self._source_address = cast(tuple[str, int] | None, source_address)
        self._ssl_context = (
            cast(ssl.SSLContext, context) if context else ssl.create_default_context()
        )
        kwargs["context"] = self._ssl_context
        super().__init__(host, **kwargs)
        self._proxy_url = proxy_url

    def connect(self) -> None:
        parsed = urlsplit(self._proxy_url)
        self.sock = socks.create_connection(
            (self.host, self.port),
            timeout=cast(int | None, self.timeout),
            source_address=self._source_address,
            proxy_type=socks.SOCKS5,
            proxy_addr=parsed.hostname,
            proxy_port=parsed.port,
            proxy_rdns=True,
            proxy_username=unquote(parsed.username) if parsed.username else None,
            proxy_password=unquote(parsed.password) if parsed.password else None,
        )
        self.sock = self._ssl_context.wrap_socket(self.sock, server_hostname=self.host)


class Socks5HTTPSHandler(HTTPSHandler):
    """Route HTTPS Steam requests through an authenticated SOCKS5 proxy."""

    def __init__(self, proxy_url: str) -> None:
        super().__init__()
        self._proxy_url = proxy_url

    def https_open(self, request: Request) -> Any:
        def connection_factory(host: str, **kwargs: Any) -> Socks5HTTPSConnection:
            return Socks5HTTPSConnection(host, proxy_url=self._proxy_url, **kwargs)

        return self.do_open(connection_factory, request)


def read_steam_response(response: Any, *, max_bytes: int = MAX_STEAM_RESPONSE_BYTES) -> str:
    """Validate the final URL and read at most one bounded Steam response body."""
    validate_steam_url(str(response.geturl()))
    body = cast(bytes, response.read(max_bytes + 1))
    if len(body) > max_bytes:
        raise SteamError("ответ Steam превышает допустимый размер")
    return body.decode("utf-8", errors="replace")


class SteamHTTPError(SteamError):
    def __init__(self, status_code: int, *, retry_after_seconds: float | None = None) -> None:
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"HTTP {status_code} от Steam")


class ProxyTunnelError(SteamError):
    pass


class ProxyNetworkError(SteamError):
    pass


class ProxyHealthPool:
    """Round-robin proxy selection with cooldown quarantine and no secret logs."""

    def __init__(
        self, proxies: list[str], *, failure_threshold: int, quarantine_seconds: float
    ) -> None:
        self.proxies = proxies
        self.failure_threshold = max(1, failure_threshold)
        self.quarantine_seconds = max(0, quarantine_seconds)
        self.failures = {proxy: 0 for proxy in proxies}
        self.quarantined_until = {proxy: 0.0 for proxy in proxies}

    def choose(
        self, index: int, *, excluded: set[str] | None = None, now: float | None = None
    ) -> str | None:
        if not self.proxies:
            return None
        excluded = excluded or set()
        now = time.monotonic() if now is None else now
        for offset in range(len(self.proxies)):
            proxy = self.proxies[(index + offset) % len(self.proxies)]
            if proxy not in excluded and self.quarantined_until[proxy] <= now:
                return proxy
        raise SteamError("все прокси временно помещены в карантин")

    def record_success(self, proxy: str | None) -> None:
        if proxy:
            self.failures[proxy] = 0

    def record_failure(
        self, proxy: str | None, error: Exception, *, now: float | None = None
    ) -> None:
        if not proxy:
            return
        now = time.monotonic() if now is None else now
        if isinstance(error, ProxyTunnelError):
            self.failures[proxy] = self.failure_threshold
        else:
            self.failures[proxy] += 1
        if self.failures[proxy] >= self.failure_threshold:
            self.quarantined_until[proxy] = now + self.quarantine_seconds
            self.failures[proxy] = 0


class AdaptiveRateController:
    """Shared, conservative start-rate controller for Steam requests."""

    TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}

    def __init__(self, *, base_delay: float, max_delay: float) -> None:
        self.base_delay = max(0, base_delay)
        self.max_delay = max(self.base_delay, max_delay)
        self.current_delay = self.base_delay
        self.next_start = 0.0
        self._lock = threading.Lock()

    def wait_turn(self) -> None:
        with self._lock:
            now = time.monotonic()
            pause = max(0.0, self.next_start - now)
            self.next_start = max(now, self.next_start) + self.current_delay
        if pause:
            time.sleep(pause)

    def record_success(self) -> None:
        with self._lock:
            self.current_delay = max(self.base_delay, self.current_delay * 0.75)

    def record_failure(self, error: Exception) -> None:
        if (
            not isinstance(error, SteamHTTPError)
            or error.status_code not in self.TRANSIENT_STATUS_CODES
        ):
            return
        with self._lock:
            retry_after = getattr(error, "retry_after_seconds", None)
            target_delay = max(self.base_delay, self.current_delay * 2, retry_after or 0)
            self.current_delay = min(self.max_delay, target_delay)
            self.next_start = max(self.next_start, time.monotonic() + self.current_delay)


def is_process_alive(pid: int) -> bool:
    """Return whether a PID still exists without treating permission as death."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # On Windows invalid/nonexistent PIDs may raise a generic OSError.
        return False
    return True


class RunLock:
    """Cross-process advisory lock with owner metadata for diagnostics and migration."""

    def __init__(self, path: Path, *, stale_minutes: float) -> None:
        self.path = path
        self.authority_path = path.with_name(path.name + ".guard")
        self.stale_minutes = stale_minutes
        self.owner_token = uuid.uuid4().hex
        self.acquired = False
        self._handle: Any = None

    @staticmethod
    def _lock_handle(handle: Any) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_handle(handle: Any) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_payload(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _write_payload(path: Path, payload: dict[str, Any]) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.authority_path, os.O_RDWR | os.O_CREAT)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            self._lock_handle(handle)
        except OSError as exc:
            try:
                handle.close()
            except OSError:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise SteamError(f"уже выполняется другой SO Engine: {self.path}") from exc

        try:
            existing = self._read_payload(self.path)
            if existing and not existing.get("released", False):
                try:
                    owner_pid = int(existing.get("pid", 0))
                except (TypeError, ValueError):
                    owner_pid = 0
                try:
                    age_seconds = time.time() - self.path.stat().st_mtime
                except OSError:
                    age_seconds = 0
                legacy_lock_is_active = is_process_alive(owner_pid) or (
                    self.stale_minutes < 0 or age_seconds <= self.stale_minutes * 60
                )
                if legacy_lock_is_active:
                    raise SteamError(f"уже выполняется другой SO Engine: {self.path}")

            payload = {
                "pid": os.getpid(),
                "owner_token": self.owner_token,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "released": False,
            }
            self._write_payload(self.path, payload)
        except Exception:
            self._unlock_handle(handle)
            handle.close()
            raise
        self._handle = handle
        self.acquired = True

    def release(self) -> None:
        if not self.acquired or self._handle is None:
            return
        handle = self._handle
        payload = self._read_payload(self.path)
        if isinstance(payload, dict) and payload.get("owner_token") == self.owner_token:
            payload["released"] = True
            payload["released_at"] = datetime.now(timezone.utc).isoformat()
            self._write_payload(self.path, payload)
        self._unlock_handle(handle)
        handle.close()
        self._handle = None
        self.acquired = False

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def is_proxy_tunnel_failure(error: URLError) -> bool:
    """Return True only for proxy billing/authentication tunnel responses."""
    return bool(
        re.search(r"Tunnel connection failed:\s*(?:402|407)\b", str(error.reason), re.IGNORECASE)
    )


def cents_to_money(cents: int) -> Decimal:
    return Decimal(cents) / Decimal(100)


def price_to_cents(value: Decimal) -> int:
    return int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))


def allocate_equal_budget_quantities(
    items: list[str], prices: dict[str, int], *, total_budget_cents: int
) -> list[int]:
    """Allocate one common integer count from the equal-budget average.

    Every priced input row receives at least one item. The existing equal-cash
    allocation is calculated first, including its bounded subset-sum remainder
    pass. Its arithmetic mean is rounded down and applied as the count for each
    row. If that common count would exceed the total budget, it is reduced to
    the highest affordable common integer. Any remaining cents then add one
    unit at a time in descending price order, skipping rows that do not fit,
    until no extra item fits the budget.
    """
    if total_budget_cents < 0:
        raise SteamError("общий бюджет не может быть отрицательным")
    if not items:
        return []

    try:
        item_prices = [prices[item_name] for item_name in items]
    except KeyError as exc:
        raise SteamError(f"нет рассчитанной цены для {exc.args[0]!r}") from exc
    if any(price_cents <= 0 for price_cents in item_prices):
        raise SteamError("цены для расчета количества должны быть положительными")

    minimum_spend = sum(item_prices)
    if minimum_spend > total_budget_cents:
        raise SteamError(
            f"бюджета не хватает даже на 1 штуку каждого предмета: "
            f"нужно ${cents_to_money(minimum_spend):.2f}"
        )

    equal_share_cents = total_budget_cents // len(items)

    def quantities_at_share(share_cents: int) -> list[int]:
        return [max(1, share_cents // price_cents) for price_cents in item_prices]

    def common_average_quantities(current_quantities: list[int]) -> list[int]:
        average_quantity = sum(current_quantities) // len(current_quantities)
        highest_affordable_common_quantity = total_budget_cents // sum(item_prices)
        common_quantity = min(average_quantity, highest_affordable_common_quantity)
        quantities_with_remainder = [common_quantity] * len(items)
        remaining_cents = total_budget_cents - common_quantity * sum(item_prices)
        price_descending_indices = sorted(
            range(len(item_prices)), key=lambda index: (-item_prices[index], index)
        )

        while True:
            added_any = False
            for index in price_descending_indices:
                price_cents = item_prices[index]
                if price_cents <= remaining_cents:
                    quantities_with_remainder[index] += 1
                    remaining_cents -= price_cents
                    added_any = True
            if not added_any:
                return quantities_with_remainder

    quantities = quantities_at_share(equal_share_cents)
    base_spend = sum(
        price_cents * quantity for price_cents, quantity in zip(item_prices, quantities)
    )
    if base_spend > total_budget_cents:
        # Mandatory one-unit rows can cost more than the nominal equal share.
        # Lower the common cash target until the floor allocation is feasible;
        # this preserves equal-share behaviour while never overspending.
        low_share = 0
        high_share = equal_share_cents
        while low_share < high_share:
            candidate_share = (low_share + high_share + 1) // 2
            candidate_quantities = quantities_at_share(candidate_share)
            candidate_spend = sum(
                price_cents * quantity
                for price_cents, quantity in zip(item_prices, candidate_quantities)
            )
            if candidate_spend <= total_budget_cents:
                low_share = candidate_share
            else:
                high_share = candidate_share - 1
        quantities = quantities_at_share(low_share)
        base_spend = sum(
            price_cents * quantity for price_cents, quantity in zip(item_prices, quantities)
        )

    remaining = total_budget_cents - base_spend
    if remaining <= 0:
        return common_average_quantities(quantities)

    # Each extra unit moves one row from the floor of its equal share to its
    # ceiling.  Bitset subset-sum finds the closest total that stays <= budget.
    estimated_state_bytes = (len(item_prices) + 1) * ((remaining // 8) + 1)
    if estimated_state_bytes > MAX_SUBSET_STATE_BYTES:
        raise SteamError("бюджет и число строк слишком велики для безопасного распределения")
    mask = (1 << (remaining + 1)) - 1
    states = [1]
    for price_cents in item_prices:
        states.append((states[-1] | (states[-1] << price_cents)) & mask)
    added_spend = states[-1].bit_length() - 1

    for index in range(len(item_prices) - 1, -1, -1):
        previous = states[index]
        price_cents = item_prices[index]
        if added_spend >= price_cents and ((previous >> (added_spend - price_cents)) & 1):
            quantities[index] += 1
            added_spend -= price_cents

    return common_average_quantities(quantities)


def summarize_budget_allocation(
    items: list[str],
    quantities: list[int],
    prices: dict[str, int],
    *,
    total_budget_cents: int,
) -> dict[str, int | dict[str, int]]:
    """Return an exact, duplicate-aware budget reconciliation for audit output."""
    if total_budget_cents < 0:
        raise SteamError("общий бюджет не может быть отрицательным")
    if len(items) != len(quantities):
        raise SteamError("предметы и количества для budget-аудита должны иметь одинаковую длину")

    count_by_item: dict[str, int] = {}
    spend_by_item_cents: dict[str, int] = {}
    actual_spend_cents = 0
    for item_name, quantity in zip(items, quantities):
        if quantity < 1:
            raise SteamError("количество для budget-аудита должно быть не меньше 1")
        try:
            price_cents = prices[item_name]
        except KeyError as exc:
            raise SteamError(f"нет рассчитанной цены для {item_name!r}") from exc
        if price_cents <= 0:
            raise SteamError("цены для budget-аудита должны быть положительными")
        row_spend_cents = quantity * price_cents
        count_by_item[item_name] = count_by_item.get(item_name, 0) + quantity
        spend_by_item_cents[item_name] = spend_by_item_cents.get(item_name, 0) + row_spend_cents
        actual_spend_cents += row_spend_cents

    if actual_spend_cents > total_budget_cents:
        raise SteamError("расчет количества превысил общий бюджет")
    return {
        "requested_budget_cents": total_budget_cents,
        "actual_spend_cents": actual_spend_cents,
        "budget_remainder_cents": total_budget_cents - actual_spend_cents,
        "priced_output_rows": len(items),
        "count_by_item": count_by_item,
        "spend_by_item_cents": spend_by_item_cents,
    }


def parse_nonnegative_money(value: str) -> Decimal:
    """Parse a non-negative CLI dollar amount without float rounding."""
    text = value.strip()
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        amount = Decimal(text)
    except Exception as exc:
        raise argparse.ArgumentTypeError("сумма должна быть числом") from exc
    if not amount.is_finite():
        raise argparse.ArgumentTypeError("сумма должна быть конечной")
    exponent = amount.as_tuple().exponent
    if not isinstance(exponent, int):
        raise argparse.ArgumentTypeError("сумма должна быть конечной")
    if exponent < -2:
        raise argparse.ArgumentTypeError(
            "сумма должна содержать не более двух знаков после разделителя"
        )
    if amount < 0:
        raise argparse.ArgumentTypeError("сумма не может быть отрицательной")
    if amount > MAX_TOTAL_BUDGET:
        raise argparse.ArgumentTypeError(f"сумма не может превышать {MAX_TOTAL_BUDGET:.2f} USD")
    return amount


def parse_nonnegative_float(value: str) -> float:
    """Parse a finite non-negative CLI duration."""

    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("значение должно быть числом") from exc
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("значение должно быть конечным числом")
    if parsed < 0:
        raise argparse.ArgumentTypeError("значение не может быть отрицательным")
    return parsed


def normalize_proxy(proxy: str) -> str:
    proxy = proxy.strip()
    if not proxy:
        raise SteamError("пустая строка прокси")

    # Формат многих продавцов прокси: host:port:user:pass
    if "://" not in proxy and "@" not in proxy:
        parts = proxy.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            proxy = f"{user}:{password}@{host}:{port}"

    if "://" not in proxy:
        proxy = "https://" + proxy
    try:
        parsed = urlsplit(proxy)
        if (
            parsed.scheme not in {"http", "https", "socks5"}
            or not parsed.hostname
            or parsed.port is None
        ):
            raise ValueError("missing scheme, host, or port")
    except ValueError as exc:
        raise SteamError("некорректный хост или порт прокси") from exc
    return proxy


def redact_proxy(proxy: str | None) -> str:
    """Return a safe diagnostic label without exposing proxy identity or secrets."""
    return "configured-proxy" if proxy else "direct"


def load_proxies(path: str | None, inline_proxies: list[str]) -> list[str]:
    candidates = list(inline_proxies)
    if path:
        for raw_line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                candidates.append(line)
    proxies: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        proxy = normalize_proxy(candidate)
        if proxy not in seen:
            seen.add(proxy)
            proxies.append(proxy)
    return proxies


def parse_item_entries(text: str) -> list[tuple[str, int]]:
    """Parse input while preserving requested purchase quantities.

    Legacy files contain ``Item Name; old_price`` and get quantity ``1``.
    The quantity-aware input form is ``Item Name; count; old_price``; the
    trailing price is intentionally ignored because Steam is queried live.
    """
    entries: list[tuple[str, int]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split(";")]
        # Поддерживаются Item; old_price, Item; count; old_price и выгрузки
        # вида 8|Item. Два столбца остаются legacy-форматом: второй столбец
        # может быть нецелой старой ценой, а не количеством.
        item_name = fields[0].strip('"')
        item_name = re.sub(r"^\d+\s*\|\s*", "", item_name)
        if item_name:
            quantity = 1
            if len(fields) >= 3:
                quantity = parse_order_count(fields[1])
                if quantity < 1:
                    raise SteamError(f"количество для {item_name!r} должно быть не меньше 1")
            entries.append((item_name, quantity))
    return entries


def parse_items(text: str) -> list[str]:
    """Backward-compatible item-name-only parser for existing callers."""
    return [item_name for item_name, _quantity in parse_item_entries(text)]


def parse_price(value: object) -> Decimal:
    if isinstance(value, (list, tuple)):
        value = value[0]
    text = unescape(str(value))
    text = re.sub(r"<[^>]+>", "", text).replace("\u00a0", " ")
    match = re.search(r"\d[\d\s,\.]*", text)
    if not match:
        raise SteamError(f"не удалось распарсить цену: {value!r}")

    raw = match.group(0).replace(" ", "")
    if "," in raw and "." in raw:
        raw = (
            raw.replace(",", "")
            if raw.rfind(".") > raw.rfind(",")
            else raw.replace(".", "").replace(",", ".")
        )
    elif "," in raw:
        raw = raw.replace(",", ".")
    return Decimal(raw)


def parse_order_count(value: object) -> int:
    text = unescape(str(value))
    text = re.sub(r"<[^>]+>", "", text).replace("\u00a0", "").replace(" ", "")
    try:
        parsed = Decimal(text.replace(",", ""))
        if parsed != parsed.to_integral_value():
            raise ValueError("fractional count")
        count = int(parsed)
    except Exception as exc:
        raise SteamError(f"количество ордеров должно быть целым: {value!r}") from exc
    if count < 0:
        raise SteamError(f"количество ордеров не может быть отрицательным: {value!r}")
    return count


def select_buy_order_from_graph(
    buy_graph: list[object], *, config: BidOrderConfig | None = None
) -> BidDecision:
    """Turn Steam's cumulative graph into the agreed wall-aware decision."""
    cumulative_levels: list[BidLevel] = []
    for row in buy_graph:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            raise SteamError(f"некорректный уровень buy_order_graph: {row!r}")
        price_cents = price_to_cents(parse_price(row[0]))
        # Steam returns [price, cumulative_count, descriptive_text].  The
        # two-element variant is likewise [price, cumulative_count].
        cumulative_count = parse_order_count(row[1])
        cumulative_levels.append(BidLevel(price_cents, cumulative_count))

    if not cumulative_levels:
        raise SteamError("Steam не вернул уровни buy_order_graph")

    try:
        levels = list(decompose_cumulative_buy_orders(cumulative_levels))
        visible_floor_cents = min(level.price_cents for level in levels)
        return choose_bid_order(
            levels,
            config=config or BidOrderConfig(),
            visible_floor_cents=visible_floor_cents,
        )
    except ValueError as exc:
        if "no positive order counts" in str(exc):
            raise SteamError("buy_order_graph не содержит положительных заявок") from exc
        raise SteamError(f"некорректный buy_order_graph: {exc}") from exc


def _retry_after_seconds(headers: Any) -> float | None:
    raw = headers.get("Retry-After") if headers else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def fetch(
    url: str,
    *,
    proxy: str | None = None,
    referer: str | None = None,
    request_headers: dict[str, str] | None = None,
    timeout: int = 30,
    retries: int = 3,
    rate_controller: AdaptiveRateController | None = None,
) -> str:
    """Fetch one HTTPS Steam Community endpoint, pacing every HTTP attempt."""
    validate_steam_url(url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": LEGACY_MARKET_COOKIE,
    }
    headers.update(request_headers if request_headers is not None else STEAM_NAVIGATION_HEADERS)
    if referer:
        headers["Referer"] = referer

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        if rate_controller:
            rate_controller.wait_turn()
        try:
            request = Request(url, headers=headers)
            if proxy and urlsplit(proxy).scheme == "socks5":
                opener = build_opener(Socks5HTTPSHandler(proxy), SteamRedirectHandler())
            else:
                proxy_mapping = {"http": proxy, "https": proxy} if proxy else {}
                opener = build_opener(ProxyHandler(proxy_mapping), SteamRedirectHandler())
            with opener.open(request, timeout=timeout) as response:
                text = read_steam_response(response)
            if rate_controller:
                rate_controller.record_success()
            return text
        except HTTPError as exc:
            last_error = exc
            error = SteamHTTPError(exc.code, retry_after_seconds=_retry_after_seconds(exc.headers))
            if rate_controller:
                rate_controller.record_failure(error)
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == retries:
                raise error from exc
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if proxy and isinstance(exc, URLError) and is_proxy_tunnel_failure(exc):
                raise ProxyTunnelError("прокси отклонил туннельное соединение") from exc
            if attempt == retries:
                if proxy:
                    raise ProxyNetworkError("ошибка сети через прокси") from exc
                raise SteamError(f"ошибка сети: {exc}") from exc
        time.sleep(1.5 * attempt)
    raise SteamError(f"не удалось получить ответ Steam: {last_error}")


def steam_market_name(item_name: str) -> str:
    # Steam market_hash_name uses the text heart (♥), not emoji variation (♥️).
    return item_name.replace("\ufe0f", "")


def listing_url(item_name: str) -> str:
    return (
        f"https://steamcommunity.com/market/listings/{APP_ID}/{quote(steam_market_name(item_name))}"
    )


def extract_item_nameid(html: str) -> str:
    patterns = (
        r"Market_LoadOrderSpread\(\s*(\d+)\s*\)",
        r"ItemActivityTicker\.Start\(\s*(\d+)\s*\)",
        r"item_nameid\s*[=:]\s*['\"]?(\d+)",
        r"['\"]item_nameid['\"]\s*:\s*['\"]?(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    raise SteamError("не найден item_nameid на странице Steam Market")


def load_cache(cache_path: Path) -> dict[str, str]:
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return (
        {str(k): str(v) for k, v in data.items() if str(v).isdigit()}
        if isinstance(data, dict)
        else {}
    )


def save_cache(cache_path: Path, cache: dict[str, str]) -> None:
    """Atomically replace the nameid cache to survive interrupted writes."""
    temporary_path = cache_path.with_name(cache_path.name + ".tmp")
    temporary_path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary_path.replace(cache_path)


def load_progress_state(progress_path: Path) -> ProgressState:
    """Load only the current checkpoint schema without trusting malformed fields."""
    empty = ProgressState({}, {}, {}, {}, {})
    if not progress_path.exists():
        return empty
    try:
        data = json.loads(progress_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SteamError(f"не удалось прочитать checkpoint: {progress_path}") from exc
    except json.JSONDecodeError as exc:
        raise SteamError("поврежденный JSON checkpoint") from exc
    if not isinstance(data, dict):
        raise SteamError("checkpoint должен содержать JSON-объект")
    if data.get("version") != CHECKPOINT_SCHEMA_VERSION:
        raise SteamError(
            f"неподдерживаемая версия checkpoint: ожидалась {CHECKPOINT_SCHEMA_VERSION}"
        )

    raw_prices = data.get("prices", {})
    raw_failures = data.get("failures", {})
    raw_price_timestamps = data.get("price_timestamps", {})
    raw_failure_timestamps = data.get("failure_timestamps", {})
    raw_metadata = data.get("metadata")
    if not isinstance(raw_metadata, dict):
        raise SteamError("неполные metadata checkpoint")
    metadata = {str(key): str(value) for key, value in raw_metadata.items()}
    if any(not metadata.get(key) for key in CHECKPOINT_METADATA_KEYS):
        raise SteamError("неполные metadata checkpoint")
    if not isinstance(raw_prices, dict):
        raw_prices = {}
    if not isinstance(raw_failures, dict):
        raw_failures = {}
    prices = {
        str(item): int(price)
        for item, price in raw_prices.items()
        if isinstance(price, int) and not isinstance(price, bool) and price > 0
    }
    return ProgressState(
        prices=prices,
        failures={str(item): str(error) for item, error in raw_failures.items()},
        price_timestamps={str(item): str(value) for item, value in raw_price_timestamps.items()}
        if isinstance(raw_price_timestamps, dict)
        else {},
        failure_timestamps={str(item): str(value) for item, value in raw_failure_timestamps.items()}
        if isinstance(raw_failure_timestamps, dict)
        else {},
        metadata=metadata,
    )


def load_progress(progress_path: Path) -> tuple[dict[str, int], dict[str, str]]:
    """Backward-compatible shorthand used by callers that need only values/errors."""
    state = load_progress_state(progress_path)
    return state.prices, state.failures


def save_progress(
    progress_path: Path,
    prices: dict[str, int],
    failures: dict[str, str],
    *,
    metadata: dict[str, str] | None = None,
    price_timestamps: dict[str, str] | None = None,
    failure_timestamps: dict[str, str] | None = None,
) -> None:
    """Atomically persist a versioned checkpoint after every attempted item."""
    metadata = metadata or {}
    if any(not metadata.get(key) for key in CHECKPOINT_METADATA_KEYS):
        raise SteamError("неполные metadata checkpoint")
    now = datetime.now(timezone.utc).isoformat()
    price_timestamps = price_timestamps or {item: now for item in prices}
    failure_timestamps = failure_timestamps or {item: now for item in failures}
    payload = {
        "version": CHECKPOINT_SCHEMA_VERSION,
        "metadata": dict(sorted((metadata or {}).items())),
        "prices": dict(sorted(prices.items())),
        "price_timestamps": dict(sorted(price_timestamps.items())),
        "failures": dict(sorted(failures.items())),
        "failure_timestamps": dict(sorted(failure_timestamps.items())),
    }
    temporary_path = progress_path.with_name(progress_path.name + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary_path.replace(progress_path)


def write_failure_report(output_path: Path, items: list[str], failures: dict[str, str]) -> None:
    """Write only unresolved unique items in their first-seen input order."""
    seen: set[str] = set()
    lines: list[str] = []
    for item_name in items:
        if item_name in seen:
            continue
        seen.add(item_name)
        if item_name in failures:
            lines.append(f"{item_name}; {failures[item_name]}")
    output_path.write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n"
    )


def write_audit_report(
    output_path: Path, metadata: dict[str, str], records: list[dict[str, object]]
) -> None:
    payload = {"metadata": metadata, "items": records}
    temporary_path = output_path.with_name(output_path.name + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary_path.replace(output_path)


def get_item_nameid(
    item_name: str,
    *,
    proxy: str | None,
    cache: dict[str, str],
    rate_controller: AdaptiveRateController | None = None,
) -> str:
    cache_key = f"{APP_ID}:{steam_market_name(item_name)}"
    if cache_key in cache:
        return cache[cache_key]

    html = fetch(listing_url(item_name), proxy=proxy, rate_controller=rate_controller)
    item_nameid = extract_item_nameid(html)
    cache[cache_key] = item_nameid
    return item_nameid


def _fetch_order_histogram(
    item_name: str,
    item_nameid: str,
    *,
    proxy: str | None,
    rate_controller: AdaptiveRateController | None = None,
) -> dict[str, object]:
    url = (
        "https://steamcommunity.com/market/itemordershistogram"
        f"?country=US&language=english&currency=1&item_nameid={item_nameid}&two_factor=0"
    )
    try:
        data = json.loads(
            fetch(
                url,
                proxy=proxy,
                referer=listing_url(item_name),
                request_headers=STEAM_HISTOGRAM_HEADERS,
                rate_controller=rate_controller,
            )
        )
    except json.JSONDecodeError as exc:
        raise SteamError("Steam вернул некорректный JSON histogram") from exc
    if not isinstance(data, dict):
        raise SteamError("Steam вернул некорректный объект histogram")
    data = cast(dict[str, object], data)
    # Steam occasionally returns success=0 for a stale nameid.  The caller
    # performs one listing refresh and a bounded retry before reporting it.
    if data.get("success") not in (None, 1, True):
        return {"buy_order_graph": [], "sell_order_graph": []}
    return data


def snapshot_from_histogram(
    data: dict[str, object], *, strategy_config: BidOrderConfig
) -> MarketSnapshot:
    """Validate a histogram response and retain both sides of the visible book."""
    buy_graph = data.get("buy_order_graph") or []
    if not isinstance(buy_graph, list) or not buy_graph:
        raise SteamError("Steam не вернул buy_order_graph")
    decision = select_buy_order_from_graph(buy_graph, config=strategy_config)

    sell_graph = data.get("sell_order_graph") or []
    if not isinstance(sell_graph, list):
        raise SteamError("Steam вернул некорректный sell_order_graph")
    sell_prices: list[int] = []
    for row in sell_graph:
        if not isinstance(row, (list, tuple)) or not row:
            raise SteamError(f"некорректный уровень sell_order_graph: {row!r}")
        sell_prices.append(price_to_cents(parse_price(row[0])))

    def response_count(field: str, graph: list[object], fallback: int) -> int:
        value = data.get(field)
        if value is not None:
            return parse_order_count(value)
        counts = [
            parse_order_count(row[1])
            for row in graph
            if isinstance(row, (list, tuple)) and len(row) > 1
        ]
        return max(counts, default=fallback)

    return MarketSnapshot(
        decision=decision,
        best_sell_cents=min(sell_prices) if sell_prices else None,
        visible_buy_orders=response_count("buy_order_count", buy_graph, decision.queue_ahead),
        visible_sell_orders=response_count("sell_order_count", sell_graph, len(sell_prices)),
    )


def get_market_snapshot(
    item_name: str,
    *,
    proxy: str | None,
    cache: dict[str, str],
    request_delay_ms: int,
    strategy_config: BidOrderConfig | None = None,
    rate_controller: AdaptiveRateController | None = None,
) -> MarketSnapshot:
    strategy_config = strategy_config or BidOrderConfig()
    item_nameid = get_item_nameid(
        item_name, proxy=proxy, cache=cache, rate_controller=rate_controller
    )
    time.sleep(max(0, request_delay_ms) / 1000)
    data = _fetch_order_histogram(
        item_name, item_nameid, proxy=proxy, rate_controller=rate_controller
    )
    if data.get("buy_order_graph"):
        return snapshot_from_histogram(data, strategy_config=strategy_config)

    # A stale item_nameid is common enough to recover in the same run.  Refresh
    # the listing page once and retry the histogram exactly once; repeated
    # empty books remain an explicit item error rather than a guessed price.
    cache_key = f"{APP_ID}:{steam_market_name(item_name)}"
    cache.pop(cache_key, None)
    html = fetch(listing_url(item_name), proxy=proxy, rate_controller=rate_controller)
    refreshed_nameid = extract_item_nameid(html)
    cache[cache_key] = refreshed_nameid
    time.sleep(max(0, request_delay_ms) / 1000)
    refreshed_data = _fetch_order_histogram(
        item_name,
        refreshed_nameid,
        proxy=proxy,
        rate_controller=rate_controller,
    )
    if refreshed_data.get("buy_order_graph"):
        return snapshot_from_histogram(refreshed_data, strategy_config=strategy_config)
    raise SteamError("Steam не вернул buy_order_graph после обновления item_nameid")


def get_buy_order_decision(
    item_name: str,
    *,
    proxy: str | None,
    cache: dict[str, str],
    request_delay_ms: int,
    strategy_config: BidOrderConfig | None = None,
) -> BidDecision:
    """Backward-compatible decision-only interface for existing callers."""
    return get_market_snapshot(
        item_name,
        proxy=proxy,
        cache=cache,
        request_delay_ms=request_delay_ms,
        strategy_config=strategy_config,
    ).decision


def input_fingerprint(items: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def strategy_fingerprint(strategy_config: BidOrderConfig, market_filter: MarketFilterConfig) -> str:
    payload = {
        "strategy": strategy_config.__dict__,
        "market_filter": market_filter.__dict__,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def safe_error_message(error: Exception) -> str:
    text = str(error)
    text = re.sub(r"(?i)(https?://)[^@\s/]+@", r"\1[REDACTED]@", text)
    return text[:500]


def default_run_directory() -> Path:
    """Use the project runtime in source checkouts and CWD for installed wheels."""

    project_root = Path(__file__).resolve().parent.parent
    if (project_root / "pyproject.toml").is_file():
        return project_root / "runtime"
    return Path.cwd() / "runtime"


def resolve_run_artifact_path(value: str | None, *, run_dir: Path) -> Path | None:
    """Put relative report/checkpoint names under one runtime directory.

    An absolute path remains an explicit user choice, useful for exporting the
    final result outside the project.
    """
    if value is None:
        return None
    candidate = Path(value)
    return candidate if candidate.is_absolute() else run_dir / candidate


def ensure_unique_artifact_paths(paths: dict[str, Path | None]) -> None:
    """Fail before processing when runtime roles or atomic sidecars collide."""
    owners_by_path: dict[str, str] = {}
    for owner, path in paths.items():
        if path is None:
            continue
        candidates = [(owner, path)]
        if owner in {"checkpoint", "cache", "audit"}:
            candidates.append((f"{owner}.tmp", path.with_name(path.name + ".tmp")))
        elif owner == "lock":
            candidates.append(("lock.guard", path.with_name(path.name + ".guard")))
        for candidate_owner, candidate_path in candidates:
            normalized = os.path.normcase(str(candidate_path.resolve(strict=False)))
            previous_owner = owners_by_path.get(normalized)
            if previous_owner is not None:
                raise SteamError(
                    f"пути {previous_owner} и {candidate_owner} совпадают: {candidate_path}"
                )
            owners_by_path[normalized] = candidate_owner


def process_items(
    items: list[str],
    *,
    proxies: list[str],
    delay: float,
    request_delay_ms: int,
    debug: bool,
    strategy_config: BidOrderConfig | None = None,
    market_filter: MarketFilterConfig | None = None,
    checkpoint_path: Path | None = None,
    resume: bool = False,
    resume_max_age_minutes: float = 60,
    resume_force: bool = False,
    max_batch_delay: float = 60,
    proxy_failure_threshold: int = 2,
    proxy_quarantine_seconds: float = 300,
    progress_every: int = 10,
    quiet: bool = False,
    failed_output_path: Path | None = None,
    skipped_output_path: Path | None = None,
    audit_path: Path | None = None,
    cache_path: Path | None = None,
    output_quantities: list[int] | None = None,
    total_budget_cents: int | None = None,
) -> BatchResult:
    """Process unique items safely and reconstruct the caller's original order."""
    if output_quantities is None:
        output_quantities = [1] * len(items)
    if len(output_quantities) != len(items) or any(quantity < 1 for quantity in output_quantities):
        raise SteamError(
            "количества результата должны соответствовать входным предметам и быть не меньше 1"
        )
    strategy_config = strategy_config or BidOrderConfig()
    market_filter = market_filter or MarketFilterConfig()
    cache_path = cache_path or default_run_directory() / "steam_item_nameid_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = load_cache(cache_path)
    run_metadata = {
        "algorithm_version": ALGORITHM_VERSION,
        "input_sha256": input_fingerprint(items),
        "strategy_sha256": strategy_fingerprint(strategy_config, market_filter),
        "country": "US",
        "currency": "1",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    unique_items = list(dict.fromkeys(items))
    current_items = set(unique_items)
    state = (
        load_progress_state(checkpoint_path)
        if checkpoint_path and resume
        else ProgressState({}, {}, {}, {}, {})
    )
    prior_hash = state.metadata.get("input_sha256")
    prior_algorithm = state.metadata.get("algorithm_version")
    prior_strategy = state.metadata.get("strategy_sha256")
    if resume and not resume_force and prior_hash and prior_hash != run_metadata["input_sha256"]:
        raise SteamError(
            "checkpoint относится к другому входному списку; используйте новый файл или --resume-force"
        )
    if resume and not resume_force and prior_algorithm and prior_algorithm != ALGORITHM_VERSION:
        raise SteamError(
            "checkpoint создан другой версией алгоритма; используйте --resume-force только осознанно"
        )
    if (
        resume
        and not resume_force
        and prior_strategy
        and prior_strategy != run_metadata["strategy_sha256"]
    ):
        raise SteamError(
            "checkpoint создан с другими параметрами стратегии; используйте новый файл или --resume-force"
        )

    reusable_prices = (
        state.reusable_prices(max_age_minutes=resume_max_age_minutes) if resume else {}
    )
    prices = {item: price for item, price in reusable_prices.items() if item in current_items}
    failures = {
        item: error for item, error in state.failures.items() if resume and item in current_items
    }
    for item_name in prices:
        failures.pop(item_name, None)
    price_timestamps = {
        item: state.price_timestamps[item] for item in prices if item in state.price_timestamps
    }
    failure_timestamps = {
        item: timestamp
        for item, timestamp in state.failure_timestamps.items()
        if resume and item in current_items
    }
    pool = ProxyHealthPool(
        proxies,
        failure_threshold=proxy_failure_threshold,
        quarantine_seconds=proxy_quarantine_seconds,
    )
    controller = AdaptiveRateController(base_delay=delay, max_delay=max_batch_delay)
    audit_records: list[dict[str, object]] = []
    skipped: dict[str, str] = {}
    successes = len(prices)

    def persist() -> None:
        run_metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_cache(cache_path, cache)
        if checkpoint_path:
            save_progress(
                checkpoint_path,
                prices,
                failures,
                metadata=run_metadata,
                price_timestamps=price_timestamps,
                failure_timestamps=failure_timestamps,
            )

    if checkpoint_path and resume:
        persist()

    for index, item_name in enumerate(unique_items, start=1):
        if item_name in prices:
            if audit_path:
                audit_records.append(
                    {
                        "item": item_name,
                        "status": "REUSED_CHECKPOINT",
                        "recommended": True,
                        "price_cents": prices[item_name],
                        "source": "fresh_checkpoint",
                        "calculated_at": price_timestamps.get(item_name),
                    }
                )
            if debug:
                print(f"# RESUME: {item_name}: reused fresh checkpoint price", file=sys.stderr)
            continue
        proxy: str | None = None
        attempted_proxies: set[str] = set()
        try:
            for proxy_attempt in range(max(1, len(proxies))):
                proxy = pool.choose(index - 1 + proxy_attempt, excluded=attempted_proxies)
                try:
                    snapshot = get_market_snapshot(
                        item_name,
                        proxy=proxy,
                        cache=cache,
                        request_delay_ms=request_delay_ms,
                        strategy_config=strategy_config,
                        rate_controller=controller,
                    )
                except SteamHTTPError as exc:
                    if exc.status_code != 429 or proxy is None:
                        raise
                    attempted_proxies.add(proxy)
                    pool.record_failure(proxy, exc)
                    if proxy_attempt + 1 == len(proxies):
                        raise
                    continue
                break
            decision = snapshot.decision
            recommendation = evaluate_market(snapshot, market_filter)
            failures.pop(item_name, None)
            failure_timestamps.pop(item_name, None)
            pool.record_success(proxy)
            calculated_at = datetime.now(timezone.utc).isoformat()
            record = {
                "item": item_name,
                "status": recommendation.status,
                "recommended": recommendation.recommended,
                "recommendation_reason": recommendation.reason,
                "price_cents": decision.price_cents,
                "top_bid_cents": decision.top_bid_cents,
                "best_sell_cents": snapshot.best_sell_cents,
                "visible_buy_orders": snapshot.visible_buy_orders,
                "visible_sell_orders": snapshot.visible_sell_orders,
                "gross_spread_bps": recommendation.gross_spread_bps,
                "net_margin_bps": recommendation.net_margin_bps,
                "band_lo_cents": decision.band_lo_cents,
                "band_hi_cents": decision.band_hi_cents,
                "mode": decision.mode,
                "reason": decision.reason,
                "queue_ahead_visible_lower_bound": decision.queue_ahead,
                "queue_estimate_confidence": "lower_bound_only",
                "walls": [
                    {"price_cents": wall.price_cents, "count": wall.count}
                    for wall in decision.walls
                ],
                "warnings": list(decision.warnings),
                "calculated_at": calculated_at,
            }
            if recommendation.recommended:
                prices[item_name] = decision.price_cents
                price_timestamps[item_name] = calculated_at
                skipped.pop(item_name, None)
                successes += 1
            else:
                prices.pop(item_name, None)
                price_timestamps.pop(item_name, None)
                skipped[item_name] = recommendation.reason
            if audit_path:
                audit_records.append(record)
            if debug:
                walls = (
                    ",".join(f"{wall.price_cents}:{wall.count}" for wall in decision.walls)
                    or "none"
                )
                print(
                    f"# {item_name}: status={recommendation.status}; top_buy_order={cents_to_money(decision.top_bid_cents):.2f}; "
                    f"buy_price={cents_to_money(decision.price_cents):.2f}; best_sell="
                    f"{cents_to_money(snapshot.best_sell_cents):.2f}"
                    if snapshot.best_sell_cents
                    else f"# {item_name}: status={recommendation.status}; buy_price={cents_to_money(decision.price_cents):.2f}; best_sell=none",
                    file=sys.stderr,
                )
                print(
                    f"# {item_name}: queue={decision.queue_ahead}; walls={walls}; proxy={redact_proxy(proxy)}",
                    file=sys.stderr,
                )
        except Exception as exc:
            message = safe_error_message(exc)
            failures[item_name] = message
            failure_timestamps[item_name] = datetime.now(timezone.utc).isoformat()
            if isinstance(exc, (ProxyTunnelError, ProxyNetworkError)):
                pool.record_failure(proxy, exc)
            if audit_path:
                audit_records.append(
                    {
                        "item": item_name,
                        "status": "ERROR",
                        "error": message,
                        "attempted_at": failure_timestamps[item_name],
                    }
                )
            print(f"ERROR: {item_name}: {message}", file=sys.stderr)
        finally:
            persist()

        if (
            not quiet
            and progress_every > 0
            and (index % progress_every == 0 or index == len(unique_items))
        ):
            print(
                f"[{index}/{len(unique_items)}] unique | success: {successes} | unresolved: {len(failures)} | "
                f"delay: {controller.current_delay:.2f}s",
                file=sys.stderr,
            )

    priced_items = [item_name for item_name in items if item_name in prices]
    allocation_summary: dict[str, int | dict[str, int]] | None = None
    if total_budget_cents is None:
        output_lines = [
            f"{item_name};{quantity};{cents_to_money(prices[item_name]):.2f}"
            for item_name, quantity in zip(items, output_quantities)
            if item_name in prices
        ]
    else:
        allocated_quantities = allocate_equal_budget_quantities(
            priced_items, prices, total_budget_cents=total_budget_cents
        )
        allocation_summary = summarize_budget_allocation(
            priced_items,
            allocated_quantities,
            prices,
            total_budget_cents=total_budget_cents,
        )
        output_lines = [
            f"{item_name};{quantity};{cents_to_money(prices[item_name]):.2f}"
            for item_name, quantity in zip(priced_items, allocated_quantities)
        ]
    unresolved = {
        item_name: failures.get(item_name, "нет цены")
        for item_name in unique_items
        if item_name not in prices and item_name not in skipped
    }
    if failed_output_path:
        write_failure_report(failed_output_path, items, unresolved)
    if skipped_output_path:
        write_failure_report(skipped_output_path, items, skipped)
    if audit_path:
        run_metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
        run_metadata["unique_total"] = str(len(unique_items))
        run_metadata["successful_unique"] = str(len(prices))
        run_metadata["skipped_unique"] = str(len(skipped))
        run_metadata["unresolved_unique"] = str(len(unresolved))
        if allocation_summary is not None:
            count_by_item = cast(dict[str, int], allocation_summary["count_by_item"])
            spend_by_item_cents = cast(dict[str, int], allocation_summary["spend_by_item_cents"])
            for record in audit_records:
                record_item = record.get("item")
                if isinstance(record_item, str) and record_item in count_by_item:
                    record["allocation_count"] = count_by_item[record_item]
                    record["allocation_spend_cents"] = spend_by_item_cents[record_item]
            run_metadata["requested_budget_cents"] = str(
                allocation_summary["requested_budget_cents"]
            )
            run_metadata["actual_spend_cents"] = str(allocation_summary["actual_spend_cents"])
            run_metadata["budget_remainder_cents"] = str(
                allocation_summary["budget_remainder_cents"]
            )
            run_metadata["priced_output_rows"] = str(allocation_summary["priced_output_rows"])
        write_audit_report(audit_path, run_metadata, audit_records)
    return BatchResult(
        lines=output_lines,
        unresolved_count=len(unresolved),
        skipped_count=len(skipped),
    )


def run_self_test() -> None:
    """Network-free readiness check for parsing, normalization, and selector wiring."""

    def check(condition: bool, label: str) -> None:
        if not condition:
            raise SteamError(f"SELF-TEST failed: {label}")

    check(
        parse_items('"AK-47 | Test"; 1.00\n# comment\n') == ["AK-47 | Test"],
        "item parsing",
    )
    check(
        parse_item_entries("AK-47 | Test;3;1.00\nM4A4 | Test; 2.50\n")
        == [("AK-47 | Test", 3), ("M4A4 | Test", 1)],
        "item quantity parsing",
    )
    check(steam_market_name("Kiss♥️Love") == "Kiss♥Love", "Steam name normalization")
    check(
        normalize_proxy("host:80:user:secret") == "https://user:secret@host:80",
        "proxy normalization",
    )
    check(
        redact_proxy("http://user:secret@host:80") == "configured-proxy",
        "proxy redaction",
    )
    decision = select_buy_order_from_graph(
        [
            ["1.00", "1", ""],
            ["0.90", "2", ""],
            ["0.88", "3", ""],
            ["0.87", "4", ""],
        ]
    )
    check(decision.price_cents > 0, "buy-order selector")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "SO Engine (Steam Order Engine): программа для нахождения лучшей цены "
            "buy order на Steam Community Market для CS2. "
            "Основная программа использует постоянный диапазон 9–13% ниже верхнего buy order."
        )
    )
    parser.add_argument("items", nargs="*", help="Названия предметов")
    parser.add_argument(
        "--items-file",
        help="Файл со строками 'Item Name; old_price' или 'Item Name; count; old_price'",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Явно запустить встроенный демонстрационный список предметов",
    )
    parser.add_argument("--output", help="Файл результата в формате 'Item Name;count;price'")
    parser.add_argument(
        "--run-dir",
        help="Папка для результатов, checkpoint, audit, cache и lock (по умолчанию: runtime рядом со скриптом)",
    )
    parser.add_argument(
        "--total-budget",
        type=parse_nonnegative_money,
        default=Decimal("20000.00"),
        help=(
            "Общий бюджет в USD: count — общее целое среднее равнобюджетных "
            "количеств после live-цен (по умолчанию: 20000)"
        ),
    )
    parser.add_argument(
        "--checkpoint",
        help="JSON-файл прогресса: успешные цены и последние ошибки уникальных предметов",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Продолжить по --checkpoint: использовать только свежие сохранённые успехи и повторить ошибки",
    )
    parser.add_argument(
        "--resume-max-age-minutes",
        type=parse_nonnegative_float,
        default=60,
        help="Максимальный возраст цены при --resume; 0 = без срока",
    )
    parser.add_argument(
        "--resume-force",
        action="store_true",
        help="Разрешить --resume при другом входном списке/версии алгоритма",
    )
    parser.add_argument("--failed-output", help="Файл нерассчитанных уникальных предметов и причин")
    parser.add_argument("--audit-file", help="JSON-аудит live-решений текущего запуска")
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10,
        help="Печатать статус каждые N уникальных предметов; 0 = выключить",
    )
    parser.add_argument("--quiet", action="store_true", help="Не печатать прогресс batch-запуска")
    parser.add_argument(
        "--max-batch-delay",
        type=parse_nonnegative_float,
        default=60,
        help="Потолок адаптивной паузы после rate limit",
    )
    parser.add_argument(
        "--proxy-failure-threshold", type=int, default=2, help="Ошибок proxy до карантина"
    )
    parser.add_argument(
        "--proxy-quarantine-seconds",
        type=parse_nonnegative_float,
        default=300,
        help="Время карантина сбойного proxy",
    )
    parser.add_argument(
        "--lock-stale-minutes",
        type=parse_nonnegative_float,
        default=120,
        help="Возраст lock-файла, после которого он считается устаревшим",
    )
    parser.add_argument("--version", action="version", version=f"SO Engine {APP_VERSION}")
    parser.add_argument(
        "--self-test", action="store_true", help="Проверить код без запросов к Steam"
    )
    parser.add_argument(
        "--describe-contract",
        action="store_true",
        help="Напечатать JSON-контракт программы без запросов к Steam",
    )
    parser.add_argument(
        "--proxy",
        action="append",
        default=[],
        help="Прокси: host:port, host:port:user:pass или user:pass@host:port",
    )
    parser.add_argument("--proxy-file", help="Файл с прокси, по одному на строку")
    parser.add_argument(
        "--delay", type=parse_nonnegative_float, default=0.7, help="Пауза между предметами"
    )
    parser.add_argument(
        "--request-delay-ms", type=int, default=500, help="Пауза между HTML и JSON запросом"
    )
    parser.add_argument(
        "--wall-min-orders",
        type=int,
        default=10,
        help="Минимум заявок для structural wall; достаточно одного из двух критериев",
    )
    parser.add_argument(
        "--wall-relative-multiplier",
        type=int,
        default=2,
        help="Множитель медианы для structural wall; достаточно одного из двух критериев",
    )

    parser.add_argument(
        "--min-spread-bps",
        type=int,
        default=0,
        help="Отсечь предметы с меньшим gross spread; 0 = выключено",
    )
    parser.add_argument(
        "--min-net-margin-bps",
        type=int,
        default=0,
        help="Отсечь предметы с меньшей маржой после комиссии; 0 = выключено",
    )
    parser.add_argument(
        "--sell-fee-bps", type=int, default=0, help="Комиссия продажи для расчёта net margin, bps"
    )
    parser.add_argument(
        "--max-queue-ahead",
        type=int,
        default=0,
        help="Лимит видимой очереди перед заявкой; 0 = выключено",
    )
    parser.add_argument(
        "--min-visible-buy-orders",
        type=int,
        default=0,
        help="Минимум видимых buy orders; 0 = выключено",
    )
    parser.add_argument(
        "--skipped-output", help="Файл предметов, отсеянных фильтрами ликвидности/маржи"
    )
    parser.add_argument("--debug", action="store_true", help="Показать top_buy_order в stderr")
    args = parser.parse_args()
    if args.describe_contract:
        print(json.dumps(contract_payload(), ensure_ascii=False, sort_keys=True))
        return 0
    if args.self_test:
        run_self_test()
        print("SELF-TEST: OK")
        return 0
    if args.items_file and args.items:
        parser.error("позиционные items и --items-file нельзя использовать вместе")
    if args.demo and (args.items_file or args.items):
        parser.error("--demo нельзя использовать вместе с items или --items-file")
    if not args.demo and not args.items_file and not args.items:
        parser.error("укажите items, --items-file или явный --demo")
    if args.resume and not args.checkpoint:
        parser.error("--resume требует путь --checkpoint")
    if (
        args.resume_max_age_minutes < 0
        or args.max_batch_delay < 0
        or args.progress_every < 0
        or args.delay < 0
        or args.request_delay_ms < 0
    ):
        parser.error("параметры задержки/возраста не могут быть отрицательными")
    if args.proxy_failure_threshold < 1 or args.proxy_quarantine_seconds < 0:
        parser.error("параметры proxy должны быть положительными")
    try:
        strategy_config = BidOrderConfig(
            wall_abs_min=args.wall_min_orders,
            wall_rel_mult=args.wall_relative_multiplier,
        )
        market_filter = MarketFilterConfig(
            min_spread_bps=args.min_spread_bps,
            min_net_margin_bps=args.min_net_margin_bps,
            sell_fee_bps=args.sell_fee_bps,
            max_queue_ahead=args.max_queue_ahead,
            min_visible_buy_orders=args.min_visible_buy_orders,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.demo:
        items = list(DEFAULT_ITEMS)
        output_quantities = [1] * len(items)
    elif args.items_file:
        try:
            items_text = Path(args.items_file).read_text(encoding="utf-8-sig")
            item_entries = parse_item_entries(items_text)
        except UnicodeError:
            parser.error("--items-file должен быть в кодировке UTF-8")
        except OSError as exc:
            parser.error(f"не удалось прочитать --items-file: {exc}")
        except SteamError as exc:
            parser.error(str(exc))
        if not item_entries:
            parser.error("--items-file не содержит предметов")
        items = [item_name for item_name, _quantity in item_entries]
        output_quantities = [quantity for _item_name, quantity in item_entries]
    else:
        items = args.items
        output_quantities = [1] * len(items)

    try:
        proxies = load_proxies(args.proxy_file, args.proxy)
    except UnicodeError:
        parser.error("--proxy-file должен быть в кодировке UTF-8")
    except OSError as exc:
        parser.error(f"не удалось прочитать --proxy-file: {exc}")
    except SteamError as exc:
        parser.error(str(exc))
    run_dir = Path(args.run_dir) if args.run_dir else default_run_directory()
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: {safe_error_message(exc)}", file=sys.stderr)
        return 1
    output_path = resolve_run_artifact_path(args.output, run_dir=run_dir)
    failed_output_path = (
        resolve_run_artifact_path(args.failed_output, run_dir=run_dir)
        if args.failed_output
        else (output_path.with_suffix(".failed.txt") if output_path else None)
    )
    skipped_output_path = (
        resolve_run_artifact_path(args.skipped_output, run_dir=run_dir)
        if args.skipped_output
        else (output_path.with_suffix(".skipped.txt") if output_path else None)
    )
    checkpoint_path = resolve_run_artifact_path(args.checkpoint, run_dir=run_dir)
    audit_path = resolve_run_artifact_path(args.audit_file, run_dir=run_dir)
    cache_path = run_dir / "steam_item_nameid_cache.json"
    lock_path = run_dir / "so_engine.run.lock"
    try:
        ensure_unique_artifact_paths(
            {
                "output": output_path,
                "failed_output": failed_output_path,
                "skipped_output": skipped_output_path,
                "checkpoint": checkpoint_path,
                "audit": audit_path,
                "cache": cache_path,
                "lock": lock_path,
                "items_input": Path(args.items_file) if args.items_file else None,
                "proxy_input": Path(args.proxy_file) if args.proxy_file else None,
            }
        )
    except SteamError as exc:
        parser.error(str(exc))
    try:
        for artifact_path in (
            output_path,
            failed_output_path,
            skipped_output_path,
            checkpoint_path,
            audit_path,
        ):
            if artifact_path:
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: {safe_error_message(exc)}", file=sys.stderr)
        return 1
    try:
        with RunLock(lock_path, stale_minutes=args.lock_stale_minutes):
            batch_result = process_items(
                items,
                proxies=proxies,
                delay=args.delay,
                request_delay_ms=args.request_delay_ms,
                debug=args.debug,
                strategy_config=strategy_config,
                market_filter=market_filter,
                checkpoint_path=checkpoint_path,
                resume=args.resume,
                resume_max_age_minutes=args.resume_max_age_minutes,
                resume_force=args.resume_force,
                max_batch_delay=args.max_batch_delay,
                proxy_failure_threshold=args.proxy_failure_threshold,
                proxy_quarantine_seconds=args.proxy_quarantine_seconds,
                progress_every=args.progress_every,
                quiet=args.quiet,
                failed_output_path=failed_output_path,
                skipped_output_path=skipped_output_path,
                audit_path=audit_path,
                cache_path=cache_path,
                output_quantities=output_quantities,
                total_budget_cents=price_to_cents(args.total_budget),
            )

            result_text = "\n".join(batch_result.lines) + ("\n" if batch_result.lines else "")
            if output_path:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(result_text, encoding="utf-8", newline="\n")
            else:
                print(result_text, end="")
    except (OSError, SteamError) as exc:
        print(f"ERROR: {safe_error_message(exc)}", file=sys.stderr)
        return 1

    return 0 if batch_result.is_complete else 3


if __name__ == "__main__":
    raise SystemExit(main())
