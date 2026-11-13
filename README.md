# zyx-agentic-llms

**Workspace** is a document-centric agent for professional knowledge work, with extensible domain expertise. It supports general conversation, source analysis, research, drafting and revision. Legal expertise is enabled by default and can be disabled.

This repository is a local development foundation. Production deployment and a second model provider remain separate work.

The frontend has a collapsible project/conversation sidebar with a persistent icon rail, a chat-first composer, and an optional Document/Sources inspector. The new-project folder chooser supplies only a name; documents are added separately. See [the frontend redesign and validation](docs/frontend-redesign.md).

## Independent local application

| Resource | Workspace | LexIntel (left unchanged) |
| --- | --- | --- |
| Frontend | http://localhost:3010 | http://localhost:3000 |
| Flask backend | http://127.0.0.1:5010 | Port 5000 |
| Redis process | 127.0.0.1:6390 | Port 6379 |
| Database | This repository's `instance/zyx-agentic-llms.db` | Its existing database |
| Accounts | Local account in Workspace's database | Its existing authentication |

Workspace has its own `.venv/`, frontend dependencies, queues, signing key, account/session store, source files, exports, Python execution files, tokenizer cache, logs, and Celery beat state. Redis persists only under this repository's `instance/redis/`. Nothing is read from the LexIntel checkout or its Python environment at runtime. Both applications can run together or independently.

Provider credentials may be copied, as authorized, into this project's private files. That reuses the provider account, not another application's running services or data. Host tools such as Node, Redis's executable, and the Docker engine remain machine prerequisites; Workspace launches its own Redis process and disposable containers under its own image/name prefix.

## Start and stop

On this machine the independent environment, private configuration, database migration, dependencies and tokenizer cache have been prepared. Run from this repository:

```sh
.venv/bin/python scripts/dev.py start
.venv/bin/python scripts/dev.py status
.venv/bin/python scripts/dev.py stop
```

`start` applies only the project-owned database migrations, then launches Redis, Flask, the agent worker, document worker, reconciler and frontend. `stop` terminates only process groups recorded by this project, after checking their start times. Occupied ports cause startup to fail rather than attach to or stop another service. Logs and PID records are under `instance/dev/`.

Open **http://localhost:3010** and create your local account on the first visit. This account is independent of LexIntel. Passwords are hashed; sessions use a project-specific signing key and HttpOnly cookie, with CSRF protection for writes. Public account creation closes after the first account is created. This local account flow does not provide email verification, password recovery, or multi-user administration.

## Set up a fresh checkout

Use Python 3.10+ and Node 22. The only application environment is `dev-monica`. Flask and React read public ports/URLs from `config/dev-monica.json`; private values belong in the ignored root `.env`.

1. Create an independent `.venv` inside this repository. Install `pip<24.1`, then `requirements.txt`; the retained legacy document parser needs the older installer's metadata handling. Do not activate or reference the LexIntel environment.
2. Install host `redis-server`, `antiword` for legacy `.doc` uploads, Node/npm, and Docker for Python/PDF tools. Local OCR additionally needs Tesseract if selected.
3. Copy `.env.example` to `.env` and fill in a newly generated `ZYX_FLASK_SECRET_KEY` plus your model/research credentials. Keep the private file accessible only to your user. The old `DATABASE_URL`, `REDIS_URL`, `FLASK_SECRET_KEY`, Cognito settings and external `PYTHONPATH` are not application configuration. Normal database overrides must point to an absolute SQLite file inside this project's `instance/`; normal Redis must use this project's configured port.
4. If cloud OCR is needed, put a private copy of AWS keys in `.private/aws/credentials` under `[workspace]` (`aws_access_key_id`, `aws_secret_access_key`, and optional `aws_session_token`). Restrict the directory to mode 700 and files to 600. The OCR client reads this file explicitly and never falls back to global AWS credentials. `ZYX_AWS_REGION` defaults to `us-east-1`.
5. Run `.venv/bin/python scripts/cache_tokenizers.py` once. This downloads public tokenizer data into the project's own cache; it makes no model request.
6. Run `npm --prefix frontend ci --legacy-peer-deps`, then start with the commands above. A fresh database receives only the retained independent migration baseline and local-auth migration. Old databases or uploads are not imported.

For Python/spreadsheet execution and PDF export, build the isolated runtime:

```sh
docker build -t zyx-agentic-llms-python-analysis:1 backend/python_runtime
```

Markdown, HTML and DOCX downloads work without Docker. Python execution and PDF export report a tool error if their runtime is unavailable. See `backend/python_runtime/README.md` for container limits.

## Configuration and storage

- `WORKSPACE_LEGAL_ENABLED=0` removes legal research tools and instructions; general source analysis and web research remain. Court export requests are rejected while disabled.
- `MODEL_PROVIDER=openai` selects the supported adapter. `WORKSPACE_FAST_MODEL` and `WORKSPACE_DEEP_MODEL` change model identifiers; they do not add another provider.
- API routes use `/api/workspace` for projects, conversations, runs, sources, authored documents and revision downloads. Local account/session routes use `/api/auth`.
- Private originals, Python runs and exports live under `instance/workspace/`. Workspace's web and worker processes share this application's durable storage and SQL database.
- The frontend generator reads only public JSON, never backend `.env` credentials. Authentication uses same-origin `/api/` through the frontend proxy to port 5010.
- `.env`, `.private/`, `.venv/`, `instance/` and runtime/test artifacts are ignored by Git. Copied obsolete settings are archived privately and never loaded.

## Verification

From the repository root:

```sh
ZYX_E2E=1 .venv/bin/python -m pytest -q -p tests.offline_guard
```

The offline guard rejects external provider access. Tests use temporary databases and provider/broker doubles and do not spend model credits. In `frontend/`:

```sh
CI=true npm test -- --watch=false --runInBand
npm run build
npm run test:e2e
```

The full mock-live workflow, including real isolated Redis/Celery and Docker checks, is documented in [tests/e2e/README.md](tests/e2e/README.md). See [local isolation verification](docs/local-isolation.md), [refactoring scope](docs/refactoring-status.md) and [architecture](docs/architecture.md). Paid-provider semantic evaluation, cloud OCR and production load testing remain separate checks.
