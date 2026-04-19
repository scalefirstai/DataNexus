# Contributing to DataNexus

Thanks for your interest. DataNexus is a contract-first reference
implementation — changes should stay faithful to the
[specification](requirements/bulk-extract-distribution-spec.md) or propose
an amendment to it.

## Ways to contribute

- **Bug fixes** — a small diff + a regression test.
- **New backends** — replace the in-memory event bus with Kafka, the local
  filesystem object store with S3, etc. Keep the module interface stable.
- **Docs & examples** — additional consumer implementations, operational
  runbooks, architecture deep-dives.
- **Spec amendments** — open an RFC-style issue describing the gap before
  submitting code.

## Dev setup

```bash
git clone https://github.com/scalefirstai/DataNexus.git
cd DataNexus

python3.12 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
pip install -r requirements-dev.txt   # pytest, black, ruff, mypy
```

### Common tasks

```bash
make install    # create venv + install deps
make test       # run pytest
make run        # start uvicorn with reload
make smoke      # end-to-end smoke script
make lint       # ruff + black --check
make format     # black + ruff --fix
```

## Tests

The suite covers the contract end-to-end against an in-process FastAPI
app. **New behavior needs a test.** If you change a module interface,
update the matching test. If you fix a bug, add a regression test that
fails without your change.

```bash
pytest -q                    # fast run
pytest -x --lf               # stop on first failure, rerun last failures
pytest --cov=extract_service # coverage (needs pytest-cov)
```

## Code style

- **Black** for formatting, **Ruff** for linting.
- Type hints on all public surfaces. Let Pydantic do validation at I/O
  boundaries.
- Small modules, no cross-layer imports (e.g. `object_store` must not
  depend on `main`).
- Prefer plain functions + explicit state over decorators-as-magic.
- Follow the spec's wording in identifiers (`extract_id`, `idempotency_key`,
  `partition_key`) so the mapping stays legible.

## Commit + PR flow

1. Branch off `main` with a descriptive name: `feat/kafka-bus`,
   `fix/idempotency-fingerprint`, `docs/runbook-outage`.
2. Commit in small, reviewable chunks. Use [Conventional Commits](https://www.conventionalcommits.org)
   prefixes: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`.
3. Make sure `make test` is green.
4. Open a PR against `main`. Fill in the template — describe *what*
   changes, *why*, and the spec section(s) it relates to.
5. One approving review is required. CI must be green before merge.

## Spec alignment

If you're adding a field, event type, or endpoint, cite the spec
section in your PR description. If the spec needs to change, say so:
the spec is in the repo and can be evolved alongside code.

## Security issues

**Do not file public issues for vulnerabilities.** See
[SECURITY.md](SECURITY.md) for the private disclosure process.

## Code of Conduct

By participating, you agree to uphold the
[Contributor Covenant](CODE_OF_CONDUCT.md).

## License

By contributing you agree that your contributions will be licensed
under the [Apache License 2.0](LICENSE).
