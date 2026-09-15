# Changelog

All notable changes to SO Engine are recorded here.

## Unreleased

### Added

- Expanded the public README with architecture, input/output contracts, budget allocation, operational controls, safety boundaries, and Steam Community Market examples.
- Added illustrative Steam Market item thumbnails under `docs/assets/steam-market/` with links to their source listings.

### Changed

- Position SO Engine as **Steam Order Engine**, a program for finding the best CS2 buy-order price on the Steam Community Market.
- Make the main CLI's 9–13% below-top-buy-order band permanent by removing custom discount-band options.
- Document the complete fixed-band FIFO/wall-aware price-selection algorithm in the public README.
- Replace per-row equal-budget quantities with a floor-average baseline, then spend the remaining budget one item at a time in descending price order.
- Update the default buy-order discount band from 8–12% to 9–13% below the current top buy order.

## 3.3.0 — 2026-08-07

### Changed

- Update the default buy-order discount band from 8–13% to 12–16% below the current top buy order.
- Advance the pricing algorithm contract to `fifo-wall-aware-v4`, so prior checkpoints cannot be resumed under the new price-selection policy.

## 3.2.0 — 2026-07-15

### Added

- Structured `BatchResult` completion state with explicit unresolved/skipped counts.
- Explicit `--demo` input mode and an editable-install smoke test that runs outside the checkout.

### Changed

- Require exactly one input source instead of silently using demonstration items.
- Return exit status `3` for incomplete batches, including filter-skipped items.
- Reject non-finite duration values, malformed/empty/non-UTF-8 item files, missing or non-UTF-8 proxy/item files, and malformed proxy entries with concise CLI errors.
- Use Hatchling exact editable mapping plus its development dependency so console/module launches work from Windows project paths containing Cyrillic characters.
- Extend the canonical verifier to exercise both editable entry points before the full release gate.
- Make Windows run-lock contention use unbuffered guard I/O and preserve the documented `SteamError` even if buffered close cleanup would fail.
- Retry removal of isolated Windows console wrappers when antivirus or the OS retains an executable handle briefly after smoke testing.
- Group automated tests, documentation, launcher support, and historical batch artifacts into dedicated directories while keeping compatibility entry points at the project root.

### Pricing compatibility

No changes were made to price formulas, discount boundaries, structural-wall logic, FIFO/queue semantics, strategy thresholds/defaults, fallback behavior, or rounding.

## 3.1.0 — 2026-07-15

### Added

- Installable `so_engine` package and `so-engine` console entry point.
- Machine-readable `--describe-contract` and human-readable `--version` commands.
- Project-owned thin Hermes skill with architecture, financial-safety, and maintenance references.
- Network-free release verifier covering tests, formatting, lint, typing, security scan, and coverage.
- Reproducible `uv.lock`, wheel/sdist build configuration, Git hygiene, and installed-wheel smoke checks.

### Changed

- Moved application orchestration to `so_engine/app.py`.
- Moved the canonical selector to `so_engine/selector.py`.
- Kept `SO Engine.py` and `bid_order_algorithm.py` as compatibility adapters.
- Preserved the source-checkout runtime directory while installed wheels use the current working directory.

### Security and reliability

- Restricted initial, redirected, and final requests to canonical HTTPS Steam Market endpoints, rejecting encoded path traversal and bounding response bodies.
- Made malformed/non-object checkpoint state fail closed and ensured force-resume rewrites filtered metadata even for all-reused batches.
- Rejected collisions between inputs, primary artifacts, atomic `.tmp` sidecars, and the lock-authority `.guard` path before processing.
- Separated persistent lock diagnostics from a stable OS-locked `.guard` inode, preserving mutual exclusion if metadata is replaced and holding ownership through final output publication.
- Bounded accepted budgets and subset-allocation state before large integer bitsets can exhaust memory.
- Included the compatibility selector adapter and `py.typed` marker in the verified wheel.

### Pricing compatibility

No intentional changes were made to price formulas, discount boundaries, structural-wall logic, FIFO/queue semantics, strategy thresholds/defaults, fallback behavior, or rounding.
