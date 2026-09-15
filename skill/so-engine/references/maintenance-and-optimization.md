# Maintenance and optimization guide

Use this reference when improving SO Engine without changing selected prices.

## Optimization order

1. Preserve the pricing contract with golden regression fixtures.
2. Fix completion and artifact safety before throughput work.
3. Refactor orchestration into testable components without output changes.
4. Improve networking and proxy failover under the existing global request-rate policy.
5. Optimize CPU/allocation only after profiling proves it material.

## Operational risks

### Checkpoint provenance

A version label alone does not identify the exact build. Store application/build identity, schema versions, input-name fingerprint, and strategy fingerprint. Reject missing or mismatched provenance by default and quarantine malformed JSON instead of treating it as empty state.

### Skipped rows

The `BatchResult` boundary distinguishes unresolved and filter-skipped rows. The CLI maps either condition to exit status `3`; still require artifact reconciliation before accepting a batch:

```text
skipped_unique == 0
unresolved_unique == 0
output rows == parsed input rows
```

### Artifact transaction

Canonicalize result, failed, skipped, checkpoint, audit, cache, lock, and temporary paths; require pairwise uniqueness on case-insensitive Windows paths. Keep the lock through budget reconciliation and atomic publication of every final artifact.

### Network and proxies

Separate HTTP retry from bounded same-item proxy failover. Handle proxy 402/407 as tunnel or ordinary HTTP failures; do not quarantine a proxy merely because Steam returned 429. Validate redirects/final URLs, bound response size, support numeric/date `Retry-After`, redact credentials, and distinguish direct/system/configured routes.

### CLI validation

Finite non-negative duration parsing, exclusive input sources, explicit demo opt-in, valid UTF-8 non-empty item/proxy files, and concise item/proxy/checkpoint failures are regression requirements. Continue converting expected config/network/filesystem failures to one redacted message and a documented nonzero status; add upper bounds where resource or wait-time abuse is material.

### Concurrency changes

Keep single-owner semantics across processes. On Windows, guard-file I/O must remain unbuffered and cleanup failures must not mask the contention `SteamError`. For race-sensitive changes, run both the deterministic guard-failure regression and the concurrent stale-lock test repeatedly before the full gate. Never trade correctness for a faster lock path.

## Safe architecture target

Keep the compatibility launchers thin while incrementally extracting:

```text
RunConfig / ArtifactPaths
SteamClient
ProxyHealthPool
AdaptiveRateController
CheckpointStore
AuditWriter
BatchProcessor
BudgetAllocator
OutputValidator
```

Inject clock, sleeper, HTTP client, and stores to test failure behavior without live Steam.

## Test priorities

Preserve golden selector fields: `price_cents`, mode, band boundaries, queue, walls, reason. Add network-free coverage for retry/redirect/proxy paths, cache refresh, corrupt state, concurrent locks, interruption, filters, atomic artifacts, CLI errors, localized money, and special market characters.

Targets: at least 95% for the deterministic selector and 85% for network/state/allocation modules and overall project after modularization.

## Performance baseline

Historical comparison only:

```text
28-item saved run: 56.889 s total, 2.032 s/item
500-row budget allocation: about 0.027 s, 13.51 MiB peak
```

Network and deliberate pacing dominate ordinary runs. Prioritize proxy failover, connection reuse, cache-aware pacing, dirty persistence, and only then bounded concurrency under one global request-start limiter. Preserve deterministic output order and pricing regression fixtures.
