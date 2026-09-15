# Adversarial release review

Use this reference before committing or deploying any SO Engine packaging, CLI, network, checkpoint, runtime-artifact, or locking change. It captures failure modes that ordinary happy-path tests miss.

## Review rule

Treat any security concern or demonstrated logic error as blocking. Suggestions may be deferred, but only with an explicit reason. Review the final staged tree; any subsequent edit invalidates the verdict.

## Network boundary

For every direct and proxied Steam request, test all three URLs independently:

1. requested URL;
2. every redirect destination;
3. final `response.geturl()`.

Require exact HTTPS `steamcommunity.com`, allowed ports, no URL credentials, and an allowlisted Market path. Direct and proxy branches must use the same redirect policy. Read at most a fixed body limit plus one byte; reject oversized responses before decoding. Regression cases must include cross-host redirect, HTTPS downgrade, hostile final URL, disallowed scheme/path, and oversized body.

## Checkpoint boundary

Checkpoint reuse is fail-closed:

- accept only the current schema version;
- require non-empty `algorithm_version`, `input_sha256`, and `strategy_sha256` on both write and load;
- compare every required value before reusing prices;
- never let `--resume-force` reinterpret an unknown schema;
- malformed or incomplete state must not silently become trusted fresh data.

Use a regression fixture with a fresh timestamp, unknown version, and empty metadata: it must never skip the live-price path by reusing the stored price.

## Artifact and input paths

Normalize paths before creating directories, acquiring a lock, loading state, or making network requests. Reject collisions across result, failed, skipped, checkpoint, audit, cache, lock, items input, and proxy input. Include an end-to-end CLI test where output and checkpoint are the same path and prove rejection happens before processing.

Keep the run lock held through final result publication, not only through item processing.

## Lock ownership

A filesystem lock needs more than `O_EXCL` creation:

- write a unique owner token with PID and creation time;
- reclaim stale locks without a check-then-delete race;
- after any reclaim race, retry exclusive creation rather than assuming ownership;
- release only if the on-disk token still belongs to this instance;
- never let an old owner delete a replacement lock.

Use deterministic tests that replace the lock after acquisition and verify the old owner cannot remove it.

## Wheel is the deliverable

Do not infer wheel contents from source-tree tests. The release gate must:

1. build wheel and sdist into a temporary directory;
2. install the wheel into a clean environment;
3. run console and module entry points;
4. run `--version`, `--describe-contract`, and `--self-test`;
5. import every compatibility module promised to users;
6. verify `py.typed` when the package claims a typed API;
7. inspect artifact contents or installed paths to ensure required files are actually shipped.

If a compatibility launcher is source-only, document that limitation instead of testing or promising it as an installed-wheel API.

## Performance guard

Budget allocation state can scale with both row count and budget magnitude. Before changing it, preserve financial invariants and benchmark memory/time with the maximum supported batch and budget. Prefer an explicit supported limit or a bounded algorithm over unbounded per-row bitset state. This does not authorize changes to protected price selection.

## Required evidence

Keep a red-green record for each bug class, then run the complete verifier. A release is ready only when the full gate passes after the final edit and a fresh independent review returns no security concerns or logic errors.
