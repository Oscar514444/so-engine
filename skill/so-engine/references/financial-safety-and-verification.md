# Financial safety and release verification

Use this reference when changing SO Engine budgeting, CLI money parsing, resume/checkpoint behavior, network fetching, or readiness checks.

## Financial invariants

For every successful budget allocation:

```text
len(quantities) == len(priced_input_rows)
all(quantity >= 1)
sum(quantity_i * price_i) <= total_budget_cents
```

If the sum of mandatory one-unit prices exceeds the budget, fail explicitly. Otherwise, never return an overspending plan.

A useful regression case is:

```text
prices = [90, 10]
budget = 100
expected quantities = [1, 1]
expected spend = 100
```

The old equal-share implementation produced `[1, 5]` and spent `140`.

After the deterministic regression test, run a seeded invariant probe across thousands of mixed-price batches. Seed the generator so failures are reproducible. Check length, minimum quantity, and spend for every case.

## CLI money parsing

Parse with `Decimal`, never binary floats or unconditional comma removal.

Accepted examples:

```text
20000
20000.00
20000,00
1.5
1,5
```

Reject negative values, `NaN`, infinities, more than two fractional digits, ambiguous thousands separators such as `20,000`, and budgets above USD 100,000.00. Before subset-sum allocation, reject any row-count/remainder combination whose bounded state would exceed the configured memory ceiling.

## Checkpoint isolation

Even with a force-resume option, filter all restored state to the current unique input set: reusable prices, failures, price timestamps, and failure timestamps. Audit `successful_unique` cannot exceed current unique inputs, and rewritten checkpoints must contain no unrelated items.

Load only the current checkpoint schema. Require a JSON object plus non-empty `algorithm_version`, `input_sha256`, and `strategy_sha256` metadata when reading or writing a checkpoint; malformed JSON, an unknown schema, or incomplete metadata must fail closed even under force-resume. Force-resume rewrites filtered state and current metadata before processing, including all-reused runs.

Resolve every input, primary artifact, atomic `.tmp` sidecar, and lock `.guard` authority path before processing and reject collisions. Hold the cross-process `RunLock` until the final output has been published. The lock file is persistent diagnostic metadata: `released: true` means no process owns it; a separate persistent `.guard` inode is the OS-lock authority so replacing metadata cannot bypass mutual exclusion.

## Self-test durability

Do not implement production readiness checks with Python `assert`; `python -O` removes them. Use explicit checks that raise a named exception and preserve the optimized-mode regression test.

## URL-opening guard

Before opening a URL, validate every requested destination, redirect, and final response URL against the required HTTPS Steam host and canonical Market endpoint shape. Reject credentials, disallowed ports, literal or repeatedly percent-decoded dot segments, encoded separators, and non-allowlisted paths. Bound redirects and response size. A static-analysis suppression is acceptable only beside a covered explicit allowlist.

## Canonical release gate

After the final edit run:

```bash
uv run python skill/so-engine/scripts/verify_project.py
```

The quick gate also checks the installed ASCII `.pth` bootstrap and runs editable module/console entry points from outside the checkout with clean stderr to expose Unicode-path bootstrap failures. The full gate performs compilation, optimized and normal self-tests, pytest, Ruff, mypy, Bandit, coverage, builds both wheel and sdist, and installs the wheel in isolation. The wheel smoke verifies version, module and console machine contracts, self-test, `bid_order_algorithm` compatibility import, and the `py.typed` marker. Do not call the tree release-ready if a later patch was not reverified.

For batch completion, require empty failed/skipped sets, matching parsed-input/output counts, and budget reconciliation from audit data; exit code alone is insufficient.
