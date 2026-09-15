# Windows Unicode editable installs

Use this reference when the SO Engine checkout path contains Cyrillic or other non-ASCII characters and source-tree tests pass while the installed console/module entry points fail outside the checkout.

## Failure mechanism

A conventional Hatchling editable wheel may write the absolute checkout path directly into a `.pth` file. On Windows, Python can decode that file with a legacy locale encoding even when the backend wrote UTF-8. The resulting mojibake path is absent from `sys.path`; running tests from the repository can hide the defect because the working directory already makes imports succeed.

## Durable configuration

Use an ASCII-only `.pth` bootstrap and keep its helper available in development:

```toml
[dependency-groups]
dev = [
  "editables~=0.3",
]

[tool.hatch.build]
dev-mode-exact = true
```

Hatchling exact editable mode generates a `.pth` line like `import _editable_impl_so_engine`; the separate Python mapping file can safely contain escaped Unicode paths. Some resolvers derive dependencies statically from `pyproject.toml`, so declare `editables` in the development group even though editable-wheel metadata may also request it.

After changing the build configuration, run `uv sync` so the lockfile, helper package, and editable wheel are refreshed.

## Verification pattern

Do not verify only from the checkout. From an unrelated temporary directory, run both entry points from the active virtual environment:

```bash
python -m so_engine --self-test
so-engine --describe-contract
```

Verify all of the following:

1. no startup warning is emitted while processing `.pth` files;
2. module self-test prints `SELF-TEST: OK`;
3. console output parses as JSON and exactly matches the expected machine contract;
4. the generated `.pth` contains an ASCII import bootstrap rather than the checkout path;
5. the isolated wheel smoke still passes, because editable success does not prove wheel correctness.

The canonical project verifier should execute this outside-checkout editable smoke even in quick mode, then execute the separate wheel install smoke in the full release gate.
