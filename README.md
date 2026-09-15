# SO Engine 3.3

CS2 Steam Market FIFO wall-aware buy-order calculator. The Python package is the only executable source of truth; the Hermes skill is a thin operational adapter.

## Architecture

```text
so_engine/
  app.py       Steam I/O, batch orchestration, state, audit and budget allocation
  selector.py  protected deterministic pricing selector
  cli.py       console entry point
  __main__.py  python -m so_engine
tests/                  automated regression and packaging tests
docs/                   changelog and project documentation
archive/batch_refresh/  preserved historical batch inputs and logs
launcher/               implementation behind the root Windows shortcut
runtime/                live run artifacts
SO Engine.py            backward-compatible launcher
bid_order_algorithm.py  backward-compatible selector imports
skill/so-engine/         project-owned Hermes skill source
```

Do not duplicate pricing formulas in the skill or helper scripts. The canonical selector is `so_engine.selector:choose_bid_order`.

## Setup

Requires Python 3.11+ and `uv`.

```bash
uv sync
uv run so-engine --self-test
```

Build and install:

```bash
uv build
uv tool install dist/so_engine-3.3.0-py3-none-any.whl
so-engine --version
```

## Machine-readable contract

```bash
uv run so-engine --describe-contract
```

The response identifies application version, protected algorithm version, audit/checkpoint schemas, output format, and canonical pricing source. The project verifier and skill use this contract instead of maintaining a second implementation.

## Input

UTF-8/UTF-8-BOM:

```text
Item Name;old_price
Item Name;count;old_price
8|Item Name;old_price
```

Old prices are comparison context and never pricing input. With the default total budget, the engine first calculates equal-budget row quantities, then replaces every input quantity with their floor arithmetic mean. If that common count would exceed the budget, it is reduced to the highest affordable integer count. The remaining budget then adds one item at a time in descending price order, skipping a row when its next item does not fit, until no next item fits. `--total-budget` accepts at most USD 100,000.00, and allocation aborts before creating an oversized subset state.

Choose exactly one input source: positional item names, `--items-file`, or the explicit `--demo` flag. Running without a source is rejected instead of silently sending requests for built-in examples; malformed, non-UTF-8, and empty/comment-only item files are also rejected. Duration options reject negative, `NaN`, and infinite values before processing starts.

## Output

Exactly:

```text
Item Name;count;price
```

Prices are handled internally as integer USD cents.

By default, the selector prices buy orders in the 9–13% discount band below the current top buy order. The CLI keeps this policy visible as `--min-discount-bps 1300` and `--max-discount-bps 900`.

Exit status `0` means every requested row was priced. Status `3` means the batch completed but at least one item was unresolved or skipped by a filter; inspect the failed/skipped artifacts and audit before consuming the output. CLI configuration/input errors use status `2`; expected runtime/checkpoint/filesystem failures use status `1` with a concise redacted diagnostic instead of a traceback.

## Standard run

```bash
uv run so-engine \
  --items-file input.txt \
  --run-dir runtime/run-20260715-180000 \
  --output result.txt \
  --checkpoint progress.json \
  --failed-output failed.txt \
  --skipped-output skipped.txt \
  --audit-file audit.json \
  --total-budget 20000.00 \
  --debug 2> runtime/run-20260715-180000/debug.log
```

Compatibility commands remain available:

```bash
python -m so_engine --self-test
python 'SO Engine.py' --self-test
```

Generated artifact names are resolved under `--run-dir`. In a source checkout the default is `runtime/` under the project; an installed wheel uses `./runtime` under the current directory. Primary, input, atomic `.tmp`, and lock-authority `.guard` paths must be unique. A persistent `so_engine.run.lock` records ownership diagnostics; `released: true` means no process owns it. A separate persistent `.guard` inode holds the authoritative OS advisory lock so replacing diagnostic metadata cannot admit a second process.

## Resume

```bash
uv run so-engine \
  --items-file input.txt \
  --run-dir runtime/run-20260715-180000 \
  --checkpoint progress.json \
  --resume \
  --output result-resumed.txt \
  --audit-file audit-resumed.json
```

Checkpoint reuse is not live market data and is labeled `REUSED_CHECKPOINT` in audit. Only the current checkpoint schema is accepted, with non-empty algorithm, input, and strategy hashes; malformed, non-object, unknown, or incomplete checkpoints fail closed. Force-resume immediately rewrites filtered state and current metadata even when every requested item is reused.

## Proxies

Prefer `--proxy-file` to command-line credentials. Supported formats include `host:port`, `host:port:user:pass`, `user:pass@host:port`, explicit HTTP/HTTPS proxy URLs, and authenticated SOCKS5 URLs such as `socks5://user:password@host:port`. Scheme-free entries use HTTPS by default; use an explicit `http://` or `socks5://` prefix only when the provider specifies that protocol. Proxy files must be UTF-8; missing files, invalid encodings, and malformed entries fail as concise configuration errors. Passwords are redacted from program diagnostics. Never commit proxy files; `.gitignore` excludes common proxy filenames.

## Verification

Quick contract/self-test check:

```bash
uv run python skill/so-engine/scripts/verify_project.py --quick
```

Full network-free release gate:

```bash
uv run python skill/so-engine/scripts/verify_project.py
```

The quick gate also executes the editable module and generated console script from outside the checkout, catching Windows Unicode-path/bootstrap failures. The full gate additionally runs compilation, normal and optimized self-tests, pytest, Ruff, mypy, Bandit, coverage, and an isolated wheel build/install smoke test covering the CLI contract, compatibility import, and typing marker.

## Development guardrail

Pricing behavior is protected. Architecture, packaging, networking, state, audit, tests, and performance may be improved only while selector regression outputs remain unchanged. Any proposed change to price formulas, discount boundaries, walls, queue semantics, fallback, rounding, or strategy defaults requires the user's explicit approval first.
