# Contributing

Thank you for your interest in contributing! This guide covers the basics
and the special case of adding printer support.

## Development setup

The project uses [**uv**](https://docs.astral.sh/uv/). A VS Code/Codespaces **dev container**
(`.devcontainer/`) provisions everything automatically; otherwise:

```bash
uv sync                       # create .venv and install all dependency groups
uv run pre-commit install     # enable lint/format/type hooks on commit
```

Run tests:
```bash
uv run pytest -m "not hardware"
```

See [docs/development.md](docs/development.md) for the full onboarding guide (project layout, running
the app with no printer, the dev harness, testing tiers, and gotchas).

## Coding standards

- **Ruff** for linting and formatting (`ruff check .` / `ruff format .`)
- **mypy** for static typing (`mypy app/`)
- No comments unless the *why* is non-obvious
- All new features need tests; aim to keep `render`/`discovery`/`loader`/`drivers` coverage ≥85%
- Follow [Conventional Commits](https://www.conventionalcommits.org/)

## License

By contributing you agree that your contribution is licensed under **GPL-3.0-or-later**
(inbound = outbound). This is required because `brother_ql` — which we import directly
— is GPL-3.0-or-later, pinning the combined work to GPL regardless of file headers.

Do **not** add AGPL-licensed or unclassified dependencies. The `license-check` CI job
will catch these and fail the PR.

## Adding a printer driver

Brother QL models work out of the box (step 1). A different brand or protocol is a new driver
(steps 2–3).

### 1. Brother QL models need no code change

The `brother_ql` driver derives every capability (supported labels, geometry, auto-cut)
from the `brother_ql_next` model and label registries at runtime — there is no capability table to
edit. Any model the library knows is accepted automatically. If your Brother QL model is missing,
it needs to be added upstream in
[`brother_ql_next`](https://github.com/LunarEclipse363/brother_ql_next); once it's released and the
lockfile is bumped (`uv lock`), it works here with no change.

### 2. If it's an entirely new driver (different protocol/brand)

Create `app/drivers/<yourdriver>.py` implementing the `PrinterDriver` Protocol:

```python
from app.drivers.base import Capability, register_driver

@register_driver("mydriver")
class MyDriver:
    CAPABILITY = Capability(...)

    def render_payload(self, png: bytes, opts: dict) -> bytes:
        ...
```

### 3. Write a fixture-based driver test (required)

Add `tests/test_drivers_<model>.py` (or extend `tests/test_drivers.py`) with:

- A `minimal_png()` fixture that produces a valid PNG at the label width
- A test that calls `driver.render_payload(png, opts)` with mocked library internals
  and asserts the correct flags are passed (model, label, cut, copies, etc.)
- A test that capability validation rejects unsupported label sizes

This test is the gate for merging new printer PRs.

## Adding a language

Label chrome words live in translation catalogs, not in templates. To add a language:

1. Copy `translations/en.yaml` to `translations/<code>.yaml` (a lowercase code such as `sv`, `cs`).
2. Translate each value. Optionally set `_date_format` / `_datetime_format` (Python `strftime`)
   to localize `{{date}}`/`{{now}}`; otherwise the day-first European default is used.
3. Do **not** put `{{…}}` in a value — catalogs are pure vocabulary; the loader rejects it.
4. `uv run pytest tests/test_i18n.py tests/test_render.py` — the validation test checks every
   `[[key]]` used by a bundled template exists in the default (`en`) catalog.

The shipped non-English catalogs (`fr`, `de`, `it`, `pt`, `nl`, `pl`) are author-supplied seeds;
native-speaker corrections are very welcome.

> **Future:** the flat-YAML catalog format is natively ingestible by translation platforms
> (Weblate, Crowdin). A hosted community-translation workflow can be wired up later — syncing to
> these same files via PRs — with no code change. It's deliberately deferred while the vocabulary
> is small.

## Submitting a PR

1. Fork and branch from `main`
2. `pytest -m "not hardware"` green
3. `ruff check` + `ruff format --check` clean
4. Fill out the pull request template
5. A maintainer will review; new printer PRs require the driver test above
