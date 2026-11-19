# Runbook — macOS local setup and operation

This runbook takes a fresh macOS machine to a running Workspace instance, then
covers the database, Docker runtime, tests and common failures. It assumes the
same host layout used for development: one repository, one local account, one
SQLite database and one project-owned Redis process.

For product behavior and agent design, see the [README](../README.md) and
[agent harness](agent-harness.md). For live evaluation suites, see the
[live suite registry](../tests/live/README.md).

## 1. Topology

| Resource | Value |
| --- | --- |
| Frontend | `http://localhost:3010` |
| Flask API | `http://127.0.0.1:5010` |
| Redis | `127.0.0.1:6390` |
| SQLite database | `instance/zyx-agentic-llms.db` |
| Public configuration | `config/dev-monica.json` |
| Private configuration | ignored root `.env` |
| Durable storage | `instance/workspace/` |
| Process logs and PID records | `instance/dev/` |
| Tokenizer cache | `instance/cache/tiktoken/` |
| Docker image | `zyx-agentic-llms-python-analysis:1` |

`scripts/dev.py` owns six processes: Redis, Flask, the agent Celery worker, the
document-preparation worker, Celery beat and the React dev server. It refuses to
start if any of those ports is already in use, and it stops only process groups it
recorded.

## 2. Prerequisites

macOS 15 on Apple silicon is the tested host. Install the Xcode command-line tools
and Homebrew first:

```sh
xcode-select --install
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Install the host tools:

```sh
brew install python@3.11 node@22 redis antiword
brew install --cask docker
```

- `python@3.11` — the project virtual environment. Homebrew keeps it keg-only, so
  `python3.11` may not be on `PATH`; use the prefix path shown below. If Homebrew
  no longer offers 3.11, create a 3.11 environment with Miniforge/conda and use
  that interpreter instead. Do not build the project environment with Python 3.12
  or newer unless the dependency set has been revalidated.
- `node@22` — React build and Playwright. Homebrew may keep it keg-only; if `node`
  is not found, run `brew link --overwrite node@22` or add
  `/opt/homebrew/opt/node@22/bin` to `PATH`.
- `redis` — the `redis-server` executable; Workspace starts its own process.
- `antiword` — legacy `.doc` extraction.
- Docker Desktop — isolated Python, LibreOffice recalculation and PDF export.
- Optional: `brew install tesseract` for local image OCR.

Verify:

```sh
"$(brew --prefix python@3.11)/bin/python3.11" --version
node --version
npm --version
redis-server --version
docker --version
docker info
```

Open Docker Desktop once after installation and wait for `docker info` to succeed.
A missing Docker engine is not a startup failure; Python execution and PDF export
return a tool error instead.

## 3. Python environment

From the repository root, create a project-local environment. Do not reuse another
project's virtual environment.

```sh
"$(brew --prefix python@3.11)/bin/python3.11" -m venv .venv
.venv/bin/python -m pip install --upgrade 'pip<24.1'
.venv/bin/python -m pip install -r requirements.txt
```

`pip<24.1` is required by the retained legacy document parser's package metadata.
If installation fails on that package, confirm the pip version before changing
anything else.

Verify imports:

```sh
.venv/bin/python -c "import flask, celery, sqlalchemy, tiktoken; print('ok')"
```

## 4. Private configuration

```sh
cp .env.example .env
chmod 600 .env
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the generated value in `ZYX_FLASK_SECRET_KEY`. Fill the rest:

| Variable | Purpose |
| --- | --- |
| `ENVIRONMENT` | Must remain `dev-monica`. |
| `ZYX_FLASK_SECRET_KEY` | New, project-specific session signing key. |
| `ZYX_REDIS_URL` | Must be `redis://127.0.0.1:6390/0`. |
| `ZYX_CELERY_QUEUE` | Must keep the `zyx-agentic-llms.dev` prefix. |
| `OPENAI_API_KEY` | Provider credential. |
| `MODEL_PROVIDER` | `openai`; no other adapter is implemented. |
| `WORKSPACE_MODEL_<ROLE>` | Optional override such as `openai:gpt-6.1-sol`. Defaults live in `config/models.json`. |
| `WORKSPACE_EFFORT_<ROLE>` | Optional reasoning-effort override. |
| `WORKSPACE_LEGAL_ENABLED` | `1` enables legal tools; `0` removes them. |
| `FREE_LAW_API_TOKEN` | Optional CourtListener token; anonymous access is rate-limited. |
| `ZYX_DATABASE_URL` | Optional absolute SQLite URL inside this project's `instance/`. |
| `ZYX_AWS_REGION` | Optional, for cloud OCR. |
| `AGENT_PYTHON_IMAGE` | Optional image override; defaults to the tag below. |

Model defaults live in `config/models.json`. There is no fast/deep mode: each run
saves one model settings snapshot. See
[model settings](model-settings.md).

Startup discards inherited `DATABASE_URL`, `REDIS_URL`, `FLASK_SECRET_KEY`,
Cognito settings and external `PYTHONPATH`. Normal database overrides must be an
absolute SQLite file inside `instance/`; Redis must use port 6390. Invalid values
fail startup instead of connecting to another application.

For cloud OCR, create `.private/aws/credentials` with a `[workspace]` profile and
mode 600; the client never falls back to global AWS credentials.

## 5. Tokenizer cache

The offline guard blocks network access, so prewarm public tokenizer data once:

```sh
.venv/bin/python scripts/cache_tokenizers.py
```

This downloads tokenizer files only; it makes no model request.

## 6. Frontend dependencies

```sh
npm --prefix frontend ci --legacy-peer-deps
```

`--legacy-peer-deps` matches the retained React 19 / react-scripts combination.
The frontend reads public configuration from `config/dev-monica.json`; it never
reads backend credentials.

## 7. Docker runtime

Build the isolated execution image:

```sh
docker build -t zyx-agentic-llms-python-analysis:1 backend/python_runtime
```

Smoke test:

```sh
docker run --rm --entrypoint /opt/python/bin/python \
  zyx-agentic-llms-python-analysis:1 -c "import pandas, openpyxl; print('ok')"
```

The image contains Python, pandas, openpyxl, LibreOffice Calc and Writer. It is
used for `python_run`, workbook recalculation, workbook checks and PDF export.
Each call starts a fresh container with its own deadline and a non-root user.

Without Docker:

- Markdown, HTML and DOCX downloads work.
- `python_run` and PDF export return a tool error.
- Spreadsheet preparation still extracts text and profiles without executing code.

Rebuild the image after changing `backend/python_runtime/`.

## 8. Database

SQLite is the authoritative store. macOS ships the `sqlite3` CLI and library, so
there is no database server to install or start. The database file, migration
history, local account, runs, steps, artifacts and conversation state all live in
`instance/zyx-agentic-llms.db`.

Migrations run automatically on start:

```sh
.venv/bin/python scripts/dev.py start
```

To run them explicitly:

```sh
.venv/bin/python -m flask --app backend.app:create_app db upgrade
```

Inspect read-only:

```sh
sqlite3 -readonly instance/zyx-agentic-llms.db ".tables"
sqlite3 -readonly instance/zyx-agentic-llms.db "select id,status,phase from workspace_runs order by created_at desc limit 5;"
sqlite3 -readonly instance/zyx-agentic-llms.db "select sequence,kind,name,state from agent_steps where run_id='<run-id>' order by sequence;"
```

Back up while the application is stopped, or use SQLite's online backup:

```sh
sqlite3 instance/zyx-agentic-llms.db ".backup 'instance/zyx-agentic-llms.backup.db'"
```

Fresh reset (only when you intend to lose local data):

```sh
.venv/bin/python scripts/dev.py stop
mv instance/zyx-agentic-llms.db instance/zyx-agentic-llms.db.before-reset
# Optional: discard queued Redis jobs too.
rm -f instance/redis/*
.venv/bin/python scripts/dev.py start
```

A fresh database receives the migration baseline and no account. Never delete or
upgrade a database belonging to another checkout.

## 9. Start, stop and first account

```sh
.venv/bin/python scripts/dev.py start
.venv/bin/python scripts/dev.py status
.venv/bin/python scripts/dev.py stop
```

Expected status lists `redis`, `backend`, `agent`, `documents`, `beat` and
`frontend` as running. Open `http://localhost:3010` and create the local account
on first visit. Account creation closes after the first account; there is no email
verification or password recovery.

Follow logs:

```sh
tail -f instance/dev/backend.log
tail -f instance/dev/agent.log
tail -f instance/dev/documents.log
tail -f instance/dev/frontend.log
```

`start` fails if a port is occupied. It does not attach to or stop the other
process.

## 10. Verification

Backend, offline (mocked providers, temporary databases, no paid calls):

```sh
ZYX_E2E=1 .venv/bin/python -m pytest -q -p tests.offline_guard
```

Backend with real isolated Redis/Celery and Docker:

```sh
ZYX_E2E=1 ZYX_REPLAY_E2E=1 ZYX_PYTHON_LIVE=1 \
  .venv/bin/python -m pytest -q -p tests.offline_guard
```

Frontend, from `frontend/`:

```sh
CI=true npm test -- --watch=false --runInBand
npm run build
npm run test:e2e
```

Current role-settings baseline (2026-11-19), Python 3.11.18:

| Check | Result |
| --- | --- |
| Offline backend | 966 passed, 20 skipped, 0 failures |
| Frontend component | Run the suite; it is the source of truth |
| Frontend build | Passed |
| Browser replay | Run the suite; it is the source of truth |

The replay suite launches its own Redis and containers. It never points at the
running local database, Redis namespace or storage.

## 11. Live evaluations

Live suites make real paid provider calls and use the signed-in local account.
Read the [live suite registry](../tests/live/README.md) first.

The three-workflow legal benchmark:

```sh
.venv/bin/python tests/live/model-benchmark/prepare.py --validate-only
.venv/bin/python tests/live/model-benchmark/prepare.py --label openai-v2 --cdp-port 64954
node tests/live/model-benchmark/browser.cjs /absolute/path/to/new-run run
.venv/bin/python tests/live/model-benchmark/collect.py /absolute/path/to/new-run
.venv/bin/python tests/live/model-benchmark/report.py /absolute/path/to/new-run
```

The browser runner connects over CDP to an already signed-in Chrome. Launch one
with a dedicated profile, for example:

```sh
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=64954 \
  --user-data-dir="$PWD/instance/browser-evaluation"
```

Sign in at `http://localhost:3010` in that window and leave the tab open. Do not
export cookies or credentials.

## 12. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Port 3010 belongs to another process` | Another app owns the port. Find it with `lsof -nP -iTCP:3010 -sTCP:LISTEN`. Stop the owner or free the port; do not change `config/dev-monica.json` casually. |
| `Create the project .venv before starting Workspace` | The venv is missing or at another path. Repeat section 3 from the repository root. |
| `Install redis-server and Node/npm on the host` | Homebrew tools are not on `PATH`. Reinstall and confirm `which redis-server npm`. |
| `Workspace must use its own local Redis port` | `ZYX_REDIS_URL` is not `127.0.0.1:6390`. Restore the template value. |
| `Local Workspace database must be a SQLite file inside this project instance directory` | `ZYX_DATABASE_URL` is relative, non-SQLite or outside `instance/`. Use an absolute path inside this checkout. |
| pip fails resolving a legacy document package | Confirm `.venv/bin/python -m pip --version` is below 24.1. |
| Tokenizer download fails | Check network access and rerun `scripts/cache_tokenizers.py`. Offline tests fail without the cache. |
| `python_run` reports Docker unavailable | Start Docker Desktop and run `docker info`; rebuild the image if it is missing. |
| PDF export fails | The Docker image or LibreOffice is missing; rebuild `backend/python_runtime`. |
| Preparation stays `summarizing` or `failed` | Check `instance/dev/documents.log`. Re-upload the file. Encrypted or image-only PDFs may need OCR. |
| A run stays `running` with no progress | Check `instance/dev/agent.log` and `scripts/dev.py status`. Use the UI Retry to resume saved steps; do not edit the database. |
| `context_budget` tool error | The selected material set is too large. Keep one research snapshot per topic plus the passages actually needed, or shorten the draft/instructions. Repeating the same selection fails again. |
| `database is locked` | More than one backend/worker set is writing, or a CLI write transaction is open. Stop stray processes and use `sqlite3 -readonly` for inspection. |
| Signed out or CSRF error after restart | Reload the page and sign in again. The signing key must stay stable in `.env`. |
| Frontend loads but API calls fail | Confirm 5010 is listening and the frontend proxy was regenerated by `npm start`. |
| `workspace processes are already running` | Run `scripts/dev.py status`, then `stop` before `start`. |
| Tests cannot reach the network | Expected under `tests.offline_guard`; prewarm tokenizers and keep provider calls mocked. |

## 13. Data layout and cleanup

| Path | Contents | Safe to delete? |
| --- | --- | --- |
| `instance/zyx-agentic-llms.db` | Runs, steps, artifacts, documents, account | No, unless resetting |
| `instance/workspace/sources` | Prepared source files | No |
| `instance/workspace/python` | Python execution files | No |
| `instance/workspace/exports` | Generated exports | No |
| `instance/redis/` | Redis append-only data | Only with a full reset |
| `instance/dev/` | Logs, PID records, beat schedule | Logs yes; `processes.json` only while stopped |
| `instance/cache/tiktoken/` | Public tokenizer cache | Yes; re-run the cache script |
| `instance/browser-evaluation/` | Dedicated Chrome profile | Yes, when not in use |
| `artifacts/` | Ignored run evidence and reports | Yes, after recording the run |
| `.env`, `.private/` | Secrets | Never commit; rotate if exposed |

Before deleting anything, confirm it belongs to this repository. Workspace never
reads another checkout's database, Redis, storage or virtual environment.
