---
name: so-engine
description: Operate the user's packaged SO Engine safely.
version: 3.3.0
author: Hermes Agent
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [steam, cs2, buy-orders, fifo, pricing]
    related_skills: [systematic-debugging, test-driven-development]
---

# SO Engine

## Purpose and routing

Use this skill for SO Engine (**Steam Order Engine**), the program for finding the best CS2 Steam Market buy-order price, FIFO/structural-wall buy orders, total-budget allocation, or `Item;count;price` output. For generic Steam data collection use the general Steam market skill instead.

The packaged Python program is the sole executable **source of truth**:

- application/orchestration: `so_engine/app.py`;
- protected selector: `so_engine/selector.py`;
- CLI: `so-engine` or `python -m so_engine`;
- compatibility launchers: `SO Engine.py`, `bid_order_algorithm.py`.

The skill routes, launches, verifies, and explains. It must not calculate a second price independently.

## Fixed pricing policy

The main program always uses a permanent **9–13% discount below the current top buy order**. It represents the band as `900`–`1300` basis points and does not expose CLI options for changing it. Other filters may skip a recommendation, but they never move the selector band.

The selector computes the lower boundary as `ceil(top_bid * 0.87)` and the upper boundary as `floor(top_bid * 0.91)` in integer cents. It decomposes Steam's cumulative buy-order graph, identifies structural walls using absolute and relative order-count thresholds, selects one cent above the highest crossable wall, and falls back to the lower band boundary when no wall can be crossed safely.

## Immutable pricing guardrail

Never modify the price-calculation algorithm without the user's explicit prior approval. Protected scope includes selector formulas and branching, discount boundaries, wall detection, queue semantics, fallback behavior, rounding, strategy thresholds/defaults, and anything that can change `price_cents`.

General requests to optimize, refactor, fix, continue, or improve do not authorize pricing changes. Non-pricing architecture may change only with regression proof that selector outputs remain identical.

## Contract preflight

From the project root run:

```bash
uv run so-engine --describe-contract
# Installed alternative:
so-engine --describe-contract
```

Require the expected algorithm version, schemas, `Item;count;price` output contract, and `so_engine.selector:choose_bid_order` pricing source. Treat a mismatch as a stop condition, not a reason to guess.

Before live work run the network-free check:

```bash
uv run so-engine --self-test
python -m so_engine --self-test
```

## Live run

Use a fresh timestamped run directory and explicit artifacts:

```bash
uv run so-engine \
  --items-file input.txt \
  --run-dir runtime/run-YYYYMMDD-HHMMSS \
  --output result.txt \
  --checkpoint progress.json \
  --failed-output failed.txt \
  --skipped-output skipped.txt \
  --audit-file audit.json \
  --debug 2> runtime/run-YYYYMMDD-HHMMSS/debug.log
```

Use `--resume` only with the matching checkpoint and label reused rows as `REUSED_CHECKPOINT`; they are not a fresh market snapshot. Never expose proxy credentials in commands, logs, reports, or chat.

## Input and output boundary

Accepted input includes `Item;old_price`, `Item;count;old_price`, and leading numeric ordinals. Choose exactly one source: positional items, `--items-file`, or explicit `--demo`; no source and malformed/empty/non-UTF-8 item files are errors. Old prices are comparison context only and never pricing input. The default CLI budget is USD 20,000: it first derives equal-budget quantities, then uses their floor arithmetic mean as the shared baseline capped to the total budget. It spends every remaining feasible cent by adding one item at a time in descending price order, skipping rows that do not fit, until no next item fits. Duration options must be finite and non-negative. Proxy files must be UTF-8; treat malformed/missing proxy input as a configuration failure and never expose credentials. Scheme-free proxy entries are normalized to HTTPS; preserve an explicit `http://` or `socks5://` prefix only when supplied by the provider.

Machine output is exactly UTF-8 `Item;count;price`, without spaces around semicolons. Do not add links or commentary inside that block.

## Completion checks

Exit status `3` means unresolved or skipped items and is always incomplete. Status `1` is an expected operational failure and status `2` a CLI/configuration failure; both stop consumption. Exit zero is necessary but not sufficient; a run is complete only when all are true:

1. `skipped_unique == 0`;
2. `unresolved_unique == 0`;
3. output row count equals parsed input row count;
4. every count is a positive integer and every price has two decimals;
5. integer-cent spend does not exceed budget;
6. audit totals, allocation fields, checkpoint, result, failed, and skipped artifacts agree.

If incomplete, report exact items and reasons. Never invent missing market prices.

## Reporting

Lead with the machine-ready block, then give concise per-item context from audit: selected price, top bid, band, crossed wall or `band_bottom`, visible queue lower bound, best sell, spread, and count. Treat all market values as a snapshot; do not promise profit or fill time. For `REUSED_CHECKPOINT`, do not fabricate unavailable live fields.

## Code maintenance

Use strict TDD for production changes. Keep compatibility launchers thin, package code authoritative, and the skill free of pricing implementation. Moving code into a package can silently change `__file__`-relative runtime paths and direct-file imports; add regression tests for both source-checkout and installed-wheel behavior before moving files. On Windows, non-ASCII checkout paths require an editable-install smoke from outside the checkout; with Hatchling, use exact editable mapping plus the `editables` development dependency so the `.pth` bootstrap stays ASCII. See `references/windows-unicode-editable-installs.md` for the reproduction and verification pattern.

Run the canonical verifier after the final edit:

```bash
uv run python skill/so-engine/scripts/verify_project.py
```

Then rebuild/install the wheel in a clean environment, run `--version`, `--describe-contract`, and `--self-test`, independently review the final staged Git diff, and only then synchronize this active Hermes skill from the project-owned source. The reviewer must explicitly probe network redirects/final URLs and response limits, checkpoint schema/metadata, artifact-path collisions, lock ownership through final publication, and actual wheel contents. Any edit after the full gate or reviewer dispatch invalidates that evidence: restage, rerun the gate, and re-dispatch review before committing or synchronizing.

See:

- `references/architecture-and-operations.md`;
- `references/financial-safety-and-verification.md`;
- `references/maintenance-and-optimization.md`;
- `references/windows-unicode-editable-installs.md`;
- `references/adversarial-release-review.md`.
