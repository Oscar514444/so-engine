# SO Engine 3.3 — Steam Order Engine

**SO Engine** means **Steam Order Engine**. It is a Windows-friendly Python program for finding the best price for a Counter-Strike 2 **buy order** on the **Steam Community Market**. It combines live order-book data, FIFO-aware queue reasoning, structural-wall detection, a permanent 9–13% pricing band, and deterministic budget allocation.

Here, “best price” means the highest admissible buy-order price selected by the protected algorithm inside the fixed discount band, while respecting visible walls and queue conditions. It is not a promise of execution, profit, or a particular fill time.

The program is a calculator and decision-support tool. It **does not log in to Steam, place orders, buy or sell items, bypass Steam controls, or promise a fill**.

## Steam Market examples

The images below are item thumbnails served by Steam's Community Market listing pages. They are illustrative examples of the kind of CS2 listings that SO Engine can inspect; prices, order-book depth, and availability are live market values and change continuously.

<table>
  <tr>
    <td align="center">
      <a href="https://steamcommunity.com/market/listings/730/AK-47%20%7C%20Redline%20%28Field-Tested%29">
        <img src="https://community.akamai.steamstatic.com/economy/image/i0CoZ81Ui0m-9KwlBY1L_18myuGuq1wfhWSaZgMttyVfPaERSR0Wqmu7LAocGIGz3UqlXOLrxM-vMGmW8VNxu5Dx60noTyLwlcK3wiFO0POlPPNSI_-RHGavzOtyufRkASq2lkxx4W-HnNyqJC3FZwYoC5p0Q7FfthW6wdWxPu-371Pdit5HnyXgznQeHYY5wyA/360fx360f" alt="AK-47 | Redline (Field-Tested) on the Steam Community Market" width="260">
      </a>
      <br>
      <sub><b>AK-47 | Redline (Field-Tested)</b></sub>
    </td>
    <td align="center">
      <a href="https://steamcommunity.com/market/listings/730/M4A1-S%20%7C%20Printstream%20%28Field-Tested%29">
        <img src="https://community.akamai.steamstatic.com/economy/image/i0CoZ81Ui0m-9KwlBY1L_18myuGuq1wfhWSaZgMttyVfPaERSR0Wqmu7LAocGIGz3UqlXOLrxM-vMGmW8VNxu5Dx60noTyL8ypexwjFS4_ega6F_H_OGMWrEwL9lj_F7Rienhgk1tjyIpYL8JSLSMxghAsBwQeMN5BHtlIblZuLr4Q3biNkRmH_5iX5Muypj47pWA6EsqPaGkUifZp-rQ1Ym/360fx360f" alt="M4A1-S | Printstream (Field-Tested) on the Steam Community Market" width="260">
      </a>
      <br>
      <sub><b>M4A1-S | Printstream (Field-Tested)</b></sub>
    </td>
  </tr>
</table>

> **Image provenance:** the thumbnails are downloaded from Steam's official Community Market CDN and linked to their corresponding Steam Market listings. They are included only to illustrate the input domain; SO Engine never treats an image as pricing data.

## Steam Market graphs: what SO Engine actually catches

The first image is a screenshot of Steam's official **Median Sale Prices** chart for [`P250 | Cartel (Field-Tested)`](https://steamcommunity.com/market/listings/730/P250%20%7C%20Cartel%20%28Field-Tested%29). It is historical sales context only. **SO Engine does not select prices from this historical chart.**

[![Steam Community Market median sale prices for P250 Cartel](https://raw.githubusercontent.com/Oscar514444/so-engine/main/docs/assets/steam-market/steam-market-history-p250-cartel-month.png)](https://steamcommunity.com/market/listings/730/P250%20%7C%20Cartel%20%28Field-Tested%29)

The selector reads Steam's public `itemordershistogram` response and its cumulative `buy_order_graph`. The next two images are annotated visualizations of that live Steam order-book response after SO Engine decomposes cumulative counts into real per-price order levels. **Red bars are the lower order-book peaks (structural walls) detected by the program; green is the selected price.**

### P250 | Cartel — walls detected and crossed

[![SO Engine catches lower buy-order peaks for P250 Cartel](https://raw.githubusercontent.com/Oscar514444/so-engine/main/docs/assets/steam-market/steam-buy-order-graph-p250-cartel-field-tested-annotated.png)](https://steamcommunity.com/market/listings/730/P250%20%7C%20Cartel%20%28Field-Tested%29)

Public snapshot shown in the image:

| Signal | Value |
|---|---:|
| Top buy order | `$11.80` |
| Fixed 9–13% band | `$10.27–$10.73` |
| Detected lower peaks | `$10.67` — 9 orders; `$10.63` — 15; `$10.52` — 17 |
| Selected price | `$10.68` (`above_wall`) |

The algorithm starts at the highest usable lower peak, checks whether one cent above it remains inside the fixed band and does not land on another detected wall, and selects `$10.68`—one cent above the `$10.67` wall.

### MP9 | Hydra — deterministic band-bottom fallback

[![SO Engine band-bottom fallback for MP9 Hydra](https://raw.githubusercontent.com/Oscar514444/so-engine/main/docs/assets/steam-market/steam-buy-order-graph-mp9-hydra-battle-scarred-annotated.png)](https://steamcommunity.com/market/listings/730/MP9%20%7C%20Hydra%20%28Battle-Scarred%29)

This second snapshot shows the other branch of the selector: top buy order `$5.58`, fixed band `$4.86–$5.07`, and **no structural walls inside the band**. SO Engine therefore selects the lower boundary `$4.86` as `band_bottom` rather than inventing a peak.

> **Important distinction:** a low point on Steam's historical sale-price chart is not automatically a buy-order wall. The red peaks above are detected only from the live buy-order depth data that the pricing selector actually consumes. Prices and order counts are volatile market snapshots, not execution or profit guarantees.

## Product algorithm: finding the best buy-order price

The main program uses one permanent pricing policy: **9–13% below the current top buy order**. The discount is represented internally in basis points as `900` to `1300`, but the CLI intentionally exposes no discount-band override.

For every requested CS2 item, SO Engine follows this sequence:

1. **Open the canonical Steam Market listing.** The engine validates the HTTPS route and reads the live listing and order-book endpoints.
2. **Read the buy-order graph.** Steam supplies cumulative counts for price levels. The engine sorts levels from the highest price down and decomposes cumulative counts into real per-level order counts.
3. **Find the top buy order.** The highest positive buy-order level becomes `top_bid_cents`. All calculations use integer cents rather than floating-point prices.
4. **Build the fixed band.** For a top order `T`, the inclusive integer-cent band is:
   - lower boundary: `ceil(T × 0.87)` — 13% below the top order;
   - upper boundary: `floor(T × 0.91)` — 9% below the top order.
5. **Inspect the levels inside the band.** The engine calculates the median order count for in-band levels. A level is treated as a structural wall when it has at least 10 orders **or** reaches at least twice the in-band median threshold.
6. **Cross the highest usable wall.** Walls are checked from the highest price down. The selected price is exactly one cent above the first wall whose next cent remains inside the band and does not land on another wall. This is the FIFO-aware “best” price: as high as the protected policy allows without placing directly into the detected wall.
7. **Use the deterministic fallback.** If no wall can be crossed safely inside the fixed band, the engine selects the lower band boundary. It records whether the decision was `above_wall` or `band_bottom` and preserves warnings for large levels above the band.
8. **Measure visible queue and market gates.** The audit records the visible queue ahead, best sell order, gross spread, and optional fee-aware net margin. Optional liquidity, queue, spread, and margin filters can mark an item as skipped; they never change the fixed 9–13% selector band.
9. **Allocate the requested budget.** After all live prices are selected, SO Engine calculates a shared quantity baseline, caps it to the total budget, and spends remaining feasible cents in descending price order while preserving input order and duplicates.
10. **Publish auditable output.** The result is written as `Item;count;price`, with checkpoint, failure, skipped-row, lock, and JSON-audit artifacts for verification and resume.

The canonical pricing implementation is:

```text
so_engine.selector:choose_bid_order
```

The Hermes skill and compatibility launchers are operational adapters; they do not contain a second pricing formula. The selector is protected by regression tests. Changes to the fixed band, wall rules, queue semantics, fallback behavior, rounding, or strategy thresholds require explicit approval before implementation.

## Budget allocation

After live prices are obtained, the default allocator:

1. calculates the established equal-budget quantities;
2. derives a shared floor-average baseline;
3. caps the baseline so the total spend remains within the requested budget;
4. distributes the remaining budget one unit at a time in descending `price_cents` order;
5. preserves input order and duplicate rows;
6. records the allocation and spend reconciliation in the audit.

The default budget is USD 20,000.00 and the CLI accepts budgets up to USD 100,000.00. A price row is never silently replaced with an invented value when Steam data is unavailable.

## Architecture

```text
so_engine/
  app.py       Steam requests, market parsing, orchestration, state, audit, allocation
  selector.py  Protected deterministic FIFO/wall-aware pricing selector
  cli.py       Console entry point
  __main__.py  python -m so_engine entry point
  py.typed     Typing marker for installed consumers

tests/         Regression, CLI, packaging, security-boundary, and contract tests
docs/          Changelog and project documentation
launcher/      Windows launcher implementation
skill/         Project-owned Hermes operational skill
SO Engine.py   Backward-compatible launcher
bid_order_algorithm.py
               Backward-compatible selector imports
```

The current machine-readable contract is:

```json
{
  "algorithm_version": "fifo-wall-aware-v4",
  "app_version": "3.3.0",
  "audit_schema_version": 1,
  "checkpoint_schema_version": 2,
  "output_format": "Item;count;price",
  "pricing_source": "so_engine.selector:choose_bid_order"
}
```

Generate the contract from the installed project instead of copying it from documentation:

```bash
uv run so-engine --describe-contract
```

## Requirements

- Windows 10/11 or another Python 3.11+ environment;
- Python 3.11 or newer;
- [`uv`](https://docs.astral.sh/uv/);
- access to the Steam Community Market for live runs.

The packaged application is network-free during self-tests. A live pricing run needs Steam connectivity and may require a legitimate, properly configured proxy pool if the current route is rate-limited.

## Installation and self-test

From the project root:

```bash
uv sync
uv run so-engine --version
uv run so-engine --describe-contract
uv run so-engine --self-test
```

Expected self-test result:

```text
SELF-TEST: OK
```

Build the package and install the wheel:

```bash
uv build
uv tool install dist/so_engine-3.3.0-py3-none-any.whl
so-engine --version
```

The compatibility entry points remain available:

```bash
python -m so_engine --self-test
python "SO Engine.py" --self-test
```

## Input format

Use exactly one input source: positional item names, `--items-file`, or the explicit `--demo` flag.

`--items-file` accepts UTF-8 or UTF-8-BOM text in any of these forms:

```text
AK-47 | Redline (Field-Tested);0.00
AK-47 | Redline (Field-Tested);4;0.00
8;AK-47 | Redline (Field-Tested);0.00
```

The old price is comparison context only. It is never used as the selector's live pricing input. Empty, malformed, non-UTF-8, and comment-only files fail closed with a concise configuration error.

## Standard live run

Use a fresh run directory and explicit artifact names:

```bash
uv run so-engine \
  --items-file input.txt \
  --run-dir runtime/run-20260915-180000 \
  --output result.txt \
  --checkpoint checkpoint.json \
  --failed-output failed.txt \
  --skipped-output skipped.txt \
  --audit-file audit.json \
  --total-budget 20000.00 \
  --debug 2> runtime/run-20260915-180000/debug.log
```

When `--run-dir` is set, relative artifact arguments are resolved under that directory. Pass artifact basenames such as `result.txt` and `checkpoint.json`; do not prefix them with the run directory a second time.

### Output contract

The machine-readable result is exactly:

```text
Item;count;price
```

Example shape:

```text
AK-47 | Redline (Field-Tested);12;37.45
M4A1-S | Printstream (Field-Tested);4;91.20
```

The example values above are format examples, not live market quotes. Prices are handled internally as integer USD cents and formatted with two decimal places.

### Run artifacts

A complete run may contain:

- `result.txt` — native `Item;count;price` output;
- `checkpoint.json` — resumable public market state and metadata;
- `audit.json` — pricing decisions, allocation, totals, and completion counts;
- `failed.txt` — unresolved unique items and redacted reasons;
- `skipped.txt` — items rejected by configured liquidity/margin filters;
- `debug.log` — optional redacted diagnostics;
- `so_engine.run.lock` and its `.guard` lock authority.

A zero exit code is necessary but not sufficient for acceptance. Before consuming output, confirm that the audit reports zero unresolved/skipped rows, the result row count matches the parsed input, every count is positive, and integer-cent spend does not exceed the budget.

## Resume and checkpoints

Resume only with the matching checkpoint and input:

```bash
uv run so-engine \
  --items-file input.txt \
  --run-dir runtime/run-20260915-180000 \
  --checkpoint checkpoint.json \
  --resume \
  --output result-resumed.txt \
  --audit-file audit-resumed.json
```

Checkpoint reuse is labeled `REUSED_CHECKPOINT` in the audit. It is not a fresh market snapshot. The engine validates algorithm, input, and strategy metadata and fails closed for malformed or incompatible checkpoint state. Use `--resume-force` only when the changed input or version has been deliberately reviewed.

## Filters and operational controls

The CLI exposes controls for:

- minimum spread and minimum net margin;
- Steam sell-fee basis points used by net-margin calculations;
- maximum visible queue ahead;
- minimum visible buy-order count;
- structural-wall minimum order count and relative multiplier;
- request and batch pacing;
- proxy quarantine threshold and quarantine duration;
- stale-lock handling;
- progress, quiet, and debug output.

See the complete interface with:

```bash
uv run so-engine --help
```

The 9–13% discount band is not a run-scoped option: the main program always uses this fixed policy. Other controls only filter or operationally manage the selected recommendations; they do not move the pricing band.

## Proxies and rate limits

Prefer a local ignored proxy file instead of putting credentials on the command line:

```bash
uv run so-engine \
  --items-file input.txt \
  --proxy-file proxies.txt
```

Supported proxy forms include `host:port`, `host:port:user:pass`, `user:pass@host:port`, explicit HTTP/HTTPS URLs, and authenticated SOCKS5 URLs. Scheme-free entries use HTTPS by default. The repository ignores `proxies*.txt`, `proxy*.txt`, `.env`, runtime output, caches, and local desktop attachments.

Proxy credentials are never intended for source control, logs, audit files, README examples, or chat. Debug output exposes only generic labels such as `configured-proxy`.

A generic HTTPS success is not proof that Steam Market requests are eligible. If Steam returns HTTP 429, diagnose the exact listing route and configured proxy paths with bounded probes before launching a long batch. Do not invent prices or alter the selector to work around an upstream rate limit.

## Verification

Quick gate:

```bash
uv run python skill/so-engine/scripts/verify_project.py --quick
```

Full network-free release gate:

```bash
uv run python skill/so-engine/scripts/verify_project.py
```

The full verifier covers compilation, normal and optimized self-tests, pytest, Ruff, mypy, Bandit, coverage, wheel build/install smoke tests, CLI contract checks, compatibility imports, and the typing marker.

## Safety and data boundaries

- Market values are snapshots, not guarantees of execution, profit, or future liquidity.
- The old price in an input file is never substituted for missing live data.
- Duplicate input rows remain duplicate output rows.
- Failed and skipped items are reported rather than silently dropped.
- Runtime artifacts and checkpoints must not contain proxy credentials, raw secret headers, or account passwords.
- The project does not automate Steam login, inventory transfers, market purchases, or order placement.
- Pricing behavior is protected; architecture and packaging changes must preserve selector regression outputs.

## Project status

Current package version: **3.3.0**

Algorithm contract: **`fifo-wall-aware-v4`**

Output contract: **`Item;count;price`**

See [`docs/CHANGELOG.md`](docs/CHANGELOG.md) for release history and compatibility notes.

## License

The repository is public for inspection and collaboration, but the project does not currently grant a permissive open-source license. Unless a license file or an explicit author grant is added, all rights remain with the author.

## Links

- [SO Engine repository](https://github.com/Oscar514444/so-engine)
- [Steam Community Market](https://steamcommunity.com/market/)
- [Counter-Strike 2 Market listings](https://steamcommunity.com/market/search?appid=730)
- [uv documentation](https://docs.astral.sh/uv/)
