"""Backward-compatible imports for the packaged SO Engine selector."""

from so_engine.selector import (
    BidDecision,
    BidLevel,
    BidOrderConfig,
    choose_bid_order,
    decompose_cumulative_buy_orders,
)

__all__ = [
    "BidDecision",
    "BidLevel",
    "BidOrderConfig",
    "choose_bid_order",
    "decompose_cumulative_buy_orders",
]
