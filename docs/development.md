# Development guide

A one-stop onboarding for new contributors. For *adding a printer driver / language* and the PR
checklist see [CONTRIBUTING.md](../CONTRIBUTING.md); for runtime/deploy config see the
[README](../README.md).

## TL;DR

```bash
uv sync                                   # create .venv, install all dependency groups
uv run pre-commit install                 # lint/format/type hooks on commit
uv run python scripts/dev_harness.py      # run the app + open a browser, token pre-filled
uv run pytest -m "not hardware"           # the test suite (no printer needed)
```

No printer required: dev uses the `file://` transport (raster written to a file instead of a
printer).

## Prerequisites

- **Python 3.13** recommended — it matches production (the Docker image and dev container both use
  3.13), so develop on the version you ship. **3.12 is the supported minimum** (`requires-python =
  ">=3.12"`, gated in CI); use it only if you must, and verify on 3.13 before shipping.
- **[uv](https://docs.astral.sh/uv/)** for dependency management and running.
- System libraries (only if running outside the dev container — the container installs them for you):
  - `libcairo2` (icon SVG rasterization), `libusb-1.0-0` (USB transport),
  - `fonts-dejavu-core` (faithful previews — same font the prod image ships),
  - `snmp` (`snmpget`/`snmpwalk`, handy for debugging a network printer's status).

## Option A — Dev container (recommended)

Open the repo in VS Code → **"Reopen in Container"** (or GitHub Codespaces). `.devcontainer/`
provisions Python 3.13 + uv, installs the system libs above, runs `uv sync`, enables pre-commit, and
installs the Playwright browser. Port **8765** is forwarded. Then:

```bash
uv run python scripts/dev_harness.py      # browser opens to the UI, ready to preview/print
```

## Option B — Local setup

```bash
# install uv: https://docs.astral.sh/uv/getting-started/installation/
uv sync                                   # .venv + all groups
uv run pre-commit install
scripts/fetch-fonts.sh                    # optional: download DejaVu into ./fonts for exact previews
```

On a bare macOS/Windows host without DejaVu, previews fall back to a different OS font (with a
one-time warning) and won't match the printed label — that's what `fetch-fonts.sh` fixes.

## Running the app

```bash
# Hot-reload dev server, no printer (file sink):
PRINTER_URI=file:///tmp/out.bin ALLOW_UNAUTHENTICATED=true \
  uv run uvicorn app.main:app --reload --port 8765
# open http://localhost:8765

# Or the dev harness (real server + browser, API token pre-filled — no --reload):
uv run python scripts/dev_harness.py            # headed browser
uv run python scripts/dev_harness.py --no-browser   # server only; open the URL yourself
uv run python scripts/dev_harness.py --check        # one-shot headless smoke (CI / agents)
```

`PRINTER_URI` scheme selects the transport: `file://<path>` (dev sink), `tcp://<host>:9100`
(network), `usb://<vendor>:<product>` (USB). See the README's *Configuration* section for the full
env-var list.

## Project layout

| Path | What lives there |
|---|---|
| `app/main.py` | FastAPI app — all HTTP routes, the print path, auth, metrics |
| `app/config.py` | `Settings` (pydantic-settings, env-driven) |
| `app/models.py` | Pydantic request/response models |
| `app/drivers/` | Printer drivers (`brother_ql.py`); capabilities derive from `brother_ql_next` |
| `app/transports/` | Delivery: `network.py` (tcp/9100), `usb.py`, `file.py`; `base.py` = the `Transport` Protocol |
| `app/render/` | Rendering engine, layout elements, i18n |
| `app/web/` | Web UI (`index.html`, `editor.html`, `history.html`) — vanilla JS, served via Jinja |
| `templates/` | Shipped label templates (YAML) |
| `translations/` | Label-chrome vocabulary catalogs (YAML) |
| `tests/` | Unit tests + `tests/e2e/` (real server + browser) |
| `docs/` | This guide, `known-limitations.md`, feature task docs |
| `scripts/` | `dev_harness.py`, `fetch-fonts.sh`, `fetch-icons.sh` |

## Testing

Tiers, controlled by markers declared in `pyproject.toml` (`hardware`, `e2e`, `slow`):

```bash
uv run pytest -m "not hardware and not icons"  # what the CI test matrix runs; mocks the printer (icons run in a dedicated CI job)
uv run pytest                       # also runs hardware-marked tests (needs a real printer)
uv run pytest --e2e -m e2e          # browser+API end-to-end (needs: uv run playwright install chromium); runs in CI as a non-blocking job
```

- **`hardware`** — touches a real printer (or live SNMP). Deselected by default; run on demand when a
  printer is reachable. New tests that hit real hardware **must** carry `@pytest.mark.hardware`;
  everything else **must mock** transport/SNMP I/O.
- **Warnings are errors** (`filterwarnings = ["error", ...]`) — keep new code/deps warning-clean.
- **Coverage** target **≥85%** on `app/` (raw network/USB transports are coverage-excluded — hardware).

## Quality gates (mirrored in CI + `.pre-commit-config.yaml`)

```bash
uv run ruff check . && uv run ruff format --check .   # lint + format (line length 100)
uv run mypy app/                                      # strict typing
```

- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/) (PR titles are linted;
  releases are automated by release-please).
- **License:** GPL-3.0-or-later, inbound=outbound (we import GPL `brother_ql`). No AGPL/unclassified
  deps — the `license-check` CI job enforces it.
- **CI Python matrix:** `3.12`/`3.13` today (a `3.14`/`3.15` forward-compat matrix is planned — see
  `docs/snmp-status-feature.md`).

## Common tasks

- **Add a label template** — drop a YAML in `templates/`; validated on load. Format reference is in
  the README's *Template format* section.
- **Add a language** — see CONTRIBUTING.md *Adding a language*.
- **Add a printer model/driver** — see CONTRIBUTING.md *Adding a printer driver* (Brother QL models
  need no code change).
- **Build artifacts** — `uv build` (wheel/sdist) · `docker build -t labelito .` (multi-stage image).

## Architecture notes & gotchas

- **Single worker by design** — print serialization (`_print_lock`) and idempotency are in-process;
  do **not** run `--workers N`. See `docs/known-limitations.md`.
- **Async coordinates, threads do blocking I/O** — routes are async; `send`/USB/SNMP run via
  `run_in_threadpool` so the event loop stays free. The transports are intentionally synchronous.
- **Printer status caveats and the SNMP plan** — `docs/known-limitations.md` and
  `docs/snmp-status-feature.md`.

## Troubleshooting

- *Previews show boxes / wrong font* → run `scripts/fetch-fonts.sh` (or install `fonts-dejavu-core`).
- *`playwright` errors on e2e / `--check`* → `uv run playwright install chromium`.
- *Port 8765 busy* → pass `--port` to the dev harness or uvicorn.
- *App refuses to start re: auth* → set `API_TOKEN=...` or `ALLOW_UNAUTHENTICATED=true` for local dev.
