from so_engine.selector import BidLevel, BidOrderConfig, choose_bid_order


CONFIG = BidOrderConfig(wall_abs_min=10, wall_rel_mult=2)


def test_default_discount_band_is_nine_to_thirteen_percent_below_top_bid():
    decision = choose_bid_order(
        [
            BidLevel(1000, 1),
            BidLevel(910, 2),
            BidLevel(900, 2),
            BidLevel(870, 10),
            BidLevel(800, 2),
        ],
        config=CONFIG,
    )

    assert (decision.band_lo_cents, decision.band_hi_cents) == (870, 910)
    assert decision.price_cents == 871
    assert decision.discount_bps == 1290


def test_selects_highest_wall_when_only_absolute_threshold_is_met():
    """A level that meets only the count threshold is still a structural wall."""
    decision = choose_bid_order(
        [
            BidLevel(1000, 1),  # top bid; outside the 9–13% band
            BidLevel(900, 5),
            BidLevel(880, 10),  # meets >=10 but not >=2*median (30)
            BidLevel(820, 100),
        ],
        config=CONFIG,
    )

    assert decision.price_cents == 881
    assert decision.mode == "above_wall"
    assert decision.top_bid_cents == 1000
    assert (decision.band_lo_cents, decision.band_hi_cents) == (870, 910)
    assert decision.queue_ahead == 6
    assert decision.discount_bps == 1190
    assert decision.reason == "Placed one cent above the first crossable structural wall at 880."
    assert decision.walls == (BidLevel(880, 10),)
    assert decision.warnings == ()


def test_selects_highest_wall_when_only_relative_threshold_is_met():
    """A level that meets only the relative-to-median threshold is still a wall."""
    decision = choose_bid_order(
        [
            BidLevel(1000, 1),  # top bid; outside the 9–13% band
            BidLevel(910, 2),
            BidLevel(900, 2),
            BidLevel(890, 9),  # meets >=2*median (4) but not >=10
            BidLevel(880, 2),
            BidLevel(860, 2),
        ],
        config=CONFIG,
    )

    assert decision.price_cents == 891
    assert decision.mode == "above_wall"
    assert [wall.price_cents for wall in decision.walls] == [890]


def test_default_wall_thresholds_match_the_configured_policy():
    config = BidOrderConfig()

    assert config.wall_abs_min == 10
    assert config.wall_rel_mult == 2
    assert (config.band_low_bps, config.band_high_bps) == (1300, 900)


def test_cascades_past_unusable_walls_until_it_can_cross_one():
    """A wall at the band ceiling and an adjacent wall must not force band bottom."""
    decision = choose_bid_order(
        [
            BidLevel(1000, 1),  # top bid; the band is 870..910
            BidLevel(910, 10),  # +1 is outside the band
            BidLevel(909, 10),  # +1 lands on the 910 wall
            BidLevel(900, 10),  # +1 is free and can be crossed
        ],
        config=CONFIG,
    )

    assert decision.price_cents == 901
    assert decision.mode == "above_wall"
    assert decision.top_bid_cents == 1000
    assert (decision.band_lo_cents, decision.band_hi_cents) == (870, 910)
    assert decision.queue_ahead == 21
    assert decision.discount_bps == 990
    assert decision.reason == "Placed one cent above the first crossable structural wall at 900."
    assert decision.walls == (BidLevel(910, 10), BidLevel(909, 10), BidLevel(900, 10))
    assert decision.warnings == ()
