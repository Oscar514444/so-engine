"""Deterministic FIFO wall-aware CS2 Steam buy-order selector.

Prices are represented exclusively as integer cents.  Steam's cumulative
``buy_order_graph`` must be decomposed before choosing a price.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class BidLevel:
    """A price level in integer cents and its order count."""

    price_cents: int
    count: int


@dataclass(frozen=True)
class BidOrderConfig:
    """Discount-band and structural-wall thresholds."""

    band_low_bps: int = 1300
    band_high_bps: int = 900
    wall_abs_min: int = 10
    wall_rel_mult: int = 2

    def __post_init__(self) -> None:
        if not 0 <= self.band_high_bps < self.band_low_bps < 10_000:
            raise ValueError("Discount band must satisfy 0 <= high < low < 10000 bps")
        if self.wall_abs_min < 1 or self.wall_rel_mult < 1:
            raise ValueError("Wall thresholds must be positive")


@dataclass(frozen=True)
class BidDecision:
    top_bid_cents: int
    band_lo_cents: int
    band_hi_cents: int
    price_cents: int
    mode: Literal["above_wall", "band_bottom"]
    reason: str
    queue_ahead: int
    discount_bps: int
    walls: tuple[BidLevel, ...]
    warnings: tuple[str, ...]

    @property
    def discount_pct(self) -> Decimal:
        """Exact presentation percentage, computed without floats."""
        return Decimal(self.top_bid_cents - self.price_cents) * 100 / Decimal(self.top_bid_cents)


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _median_times_two(values: list[int]) -> int:
    """Return twice the median so threshold comparisons remain integer-only."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle] * 2
    return ordered[middle - 1] + ordered[middle]


def _validate_levels(levels: list[BidLevel]) -> None:
    if not levels:
        raise ValueError("The bid order book is empty")
    for level in levels:
        if level.price_cents <= 0:
            raise ValueError("Bid prices must be positive integer cents")
        if level.count < 0:
            raise ValueError("Bid counts cannot be negative")


def decompose_cumulative_buy_orders(cumulative_levels: list[BidLevel]) -> tuple[BidLevel, ...]:
    """Convert cumulative Steam order counts into real per-level counts.

    Steam reports the number of orders at a price or higher.  The input may be
    unsorted; it is normalized from highest to lowest price.  Negative
    differences from a malformed/non-monotonic response are clipped to zero.
    """
    _validate_levels(cumulative_levels)
    ordered = sorted(cumulative_levels, key=lambda level: level.price_cents, reverse=True)
    if len({level.price_cents for level in ordered}) != len(ordered):
        raise ValueError("Cumulative graph contains duplicate price levels")

    previous_cumulative = 0
    result: list[BidLevel] = []
    for level in ordered:
        result.append(BidLevel(level.price_cents, max(0, level.count - previous_cumulative)))
        previous_cumulative = level.count
    return tuple(result)


def choose_bid_order(
    levels: list[BidLevel],
    *,
    config: BidOrderConfig = BidOrderConfig(),
    visible_floor_cents: int | None = None,
) -> BidDecision:
    """Select the agreed FIFO wall-aware buy-order ceiling.

    The result never exceeds the hard 9–13% discount band. It tries structural
    walls from highest to lowest, selecting one cent above the first wall whose
    candidate remains inside the band and does not land on another wall. If no
    such wall exists, it selects the bottom of the band.
    """
    _validate_levels(levels)
    ordered = sorted(
        (level for level in levels if level.count > 0),
        key=lambda level: level.price_cents,
        reverse=True,
    )
    if not ordered:
        raise ValueError("The bid order book contains no positive order counts")
    top = ordered[0].price_cents
    band_lo = _ceil_div(top * (10_000 - config.band_low_bps), 10_000)
    band_hi = top * (10_000 - config.band_high_bps) // 10_000
    if band_lo > band_hi:
        raise ValueError("Configured discount band contains no integer-cent price")

    in_band = [
        level for level in ordered if band_lo <= level.price_cents <= band_hi and level.count > 0
    ]
    walls: tuple[BidLevel, ...] = ()
    if in_band:
        twice_median = _median_times_two([level.count for level in in_band])
        walls = tuple(
            level
            for level in in_band
            if (
                level.count >= config.wall_abs_min
                or level.count * 2 >= config.wall_rel_mult * twice_median
            )
        )

    wall_prices = {wall.price_cents for wall in walls}
    crossed_wall = next(
        (
            wall
            for wall in walls
            if wall.price_cents + 1 <= band_hi and wall.price_cents + 1 not in wall_prices
        ),
        None,
    )
    if crossed_wall:
        price = crossed_wall.price_cents + 1
        mode: Literal["above_wall", "band_bottom"] = "above_wall"
        reason = f"Placed one cent above the first crossable structural wall at {crossed_wall.price_cents}."
    else:
        price = band_lo
        mode = "band_bottom"
        if walls:
            reason = (
                "No structural wall can be crossed within the discount band without "
                "landing on another structural wall; used band bottom."
            )
        else:
            reason = "No structural wall exists inside the discount band; used band bottom."

    warnings: list[str] = []
    for level in ordered:
        if level.price_cents > band_hi and level.count >= config.wall_abs_min:
            discount_bps = (top - level.price_cents) * 10_000 // top
            warnings.append(
                f"Large level at {level.price_cents} ({level.count} orders, {discount_bps} bps discount) "
                "is above the discount band and was not crossed."
            )
    if visible_floor_cents is not None and price < visible_floor_cents:
        warnings.append(
            f"Selected price {price} is below visible floor {visible_floor_cents}; visible queue may be underestimated."
        )

    queue_ahead = sum(level.count for level in ordered if level.price_cents >= price)
    discount_bps = (top - price) * 10_000 // top
    return BidDecision(
        top_bid_cents=top,
        band_lo_cents=band_lo,
        band_hi_cents=band_hi,
        price_cents=price,
        mode=mode,
        reason=reason,
        queue_ahead=queue_ahead,
        discount_bps=discount_bps,
        walls=walls,
        warnings=tuple(warnings),
    )
