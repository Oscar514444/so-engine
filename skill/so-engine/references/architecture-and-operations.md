# Architecture and operations

## Ownership boundary

The Python package is the only executable implementation. The Hermes skill is a thin adapter that selects commands, verifies artifacts, and explains results.

```text
so_engine/app.py       orchestration, Steam I/O, state, budget and audit
so_engine/selector.py  protected deterministic pricing selector
so_engine/cli.py       stable console entry point
SO Engine.py           legacy launcher only
bid_order_algorithm.py legacy import compatibility only
skill/so-engine/       project-owned Hermes skill source
```

Do not copy selector logic into the skill or its helper scripts. Helpers may launch, validate, render, or synchronize; they must consume program output rather than recompute prices.

## Versioned contract

Use `so-engine --describe-contract` as the machine-readable compatibility boundary. Application, pricing algorithm, audit schema, checkpoint schema, skill version, and output schema are separate concepts even when some currently share a release number.

The release verifier checks that the project skill version and canonical pricing source agree with the program contract. A mismatch blocks live operation.

## Launchers

Preferred source-checkout command:

```bash
uv run so-engine --help
```

Installed command:

```bash
so-engine --help
```

Portable module form:

```bash
python -m so_engine --help
```

`SO Engine.py` remains for old batch files. New automation should use the console or module entry point.

## Runtime ownership

In a source checkout, generated state goes to the project `runtime/` directory. An installed wheel uses `./runtime` under the current working directory unless `--run-dir` is supplied. Production automation should always supply an explicit, unique run directory.

## Skill synchronization

The canonical skill source lives under `skill/so-engine/` in this repository. The active Hermes copy is installed separately and must match the project source. After changing the project skill, run its tests and verifier before synchronizing it to Hermes. Never edit pricing behavior as part of synchronization.
