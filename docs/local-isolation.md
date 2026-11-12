# Independent Workspace local runtime

Workspace is served at **http://localhost:3010** from this repository. LexIntel remains at its original ports and was not modified.

## Application resources

| Resource | Workspace ownership |
| --- | --- |
| Frontend | Own React app and node_modules; port 3010; same-origin API proxy to 5010 |
| Backend | Own Flask process on 127.0.0.1:5010 |
| Redis | Dedicated process on 127.0.0.1:6390; append-only persistence under `instance/redis/` |
| SQL database | Own `instance/zyx-agentic-llms.db`; independent migration history and local account records |
| Workers | Own agent and document Celery workers, queue namespace and worker names |
| Scheduling | Own beat process and schedule/PID files under `instance/dev/` |
| Python dependencies | Physical independent `.venv/`; no external site-package paths or original-project PYTHONPATH |
| Storage | Own uploads/sources, document revisions/exports, Python execution files and logs under `instance/workspace/` |
| Tokenizers | Own `instance/cache/tiktoken/`; public tokenizer data can be prepared before offline tests |
| Identity | Local password hashes; unique session cookie/signing key; CSRF protection; no Cognito dependency |
| Secrets | Own ignored `.env` and `.private/aws/credentials` profile `[workspace]`; private files mode 600 |
| Python/PDF execution | Own image tag `zyx-agentic-llms-python-analysis:1` and uniquely named disposable containers |

The Python environment was copied offline with package files, rather than pointing at the original environment. Node, the Redis executable and Docker are host prerequisites; application services and mutable data are separate. Authorized copies of provider credentials may use the same provider account. No global AWS profile is consulted by cloud OCR, and no original-project login is used.

Previous private settings and unused copied databases are archived outside the project, together with a backup taken before the local-account schema cleanup. They are never loaded. Runtime configuration reads only this repository's `.env`; startup discards inherited provider, Flask, Redis/Celery, workspace and Python-path settings. Normal database configuration rejects files outside this repository's `instance/`; Redis configuration rejects another application's port.

The Flask/Redis/Celery/SQLAlchemy/Docker system design and agent execution loop remain in place. Local account/session handling replaces the shared external user pool. The first browser visit creates the one local owner account; email verification, recovery, multi-user administration and production authentication hardening remain future work.

## Lifecycle

From this repository:

```sh
.venv/bin/python scripts/dev.py start
.venv/bin/python scripts/dev.py status
.venv/bin/python scripts/dev.py stop
```

The process manager records only its own process groups and checks PID start times before stopping them. It refuses occupied application ports. It does not attach to, stop, migrate or delete another application's resources. Web and worker processes use Workspace's own shared database/storage, as required by the preserved system design.

Open http://localhost:3010 and choose your account credentials. The live database has been left without a test account or test documents.

## Verification

The final local checks passed on November 12, 2026:

- **469 backend tests, zero skipped**, including real isolated Redis/Celery replay, Docker execution/PDF checks, local authentication, CSRF/session boundaries, rejected shared-resource configuration, and inherited-environment isolation. Total run time: 102.96 seconds.
- **86 frontend component tests** across eight suites; production build passed.
- **54 mocked Chromium workflows** including cancel, reload, retry, stale responses, source preparation, saved results and downloads.
- **Real application browser smoke:** port 3010 rendered the initial account form, proxied its session handshake to the real backend with HTTP 200, restored the form after reload, and received the project-named HttpOnly session cookie. No browser errors. No live account was created.
- **Independent stop/start:** Workspace ports 3010/5010/6390 closed and reopened; LexIntel's listener PIDs on 3000/5000/6379 were unchanged throughout. All six Workspace services were left running.
- Dedicated Redis data directory, private credential permissions, project-owned dependency paths, ignored/untracked credential/runtime files, Python syntax, CI YAML and diff whitespace checks passed.

Reports are under ignored `artifacts/workspace-isolation/`; browser replay traces/results also use `artifacts/workspace-replay/`. CI prewarms public tokenizer data before enabling the external-network guard. These checks made no paid model request or cloud OCR call. Production load testing, cloud OCR acceptance and paid-provider semantic evaluation were not part of this change. No commits or pushes were made.

## Database and Word add-in cleanup

The follow-up audit confirmed exactly one local owner account and no legacy-branded stored data. Migration `0003_local_accounts_only` removes the unused external identity column while preserving IDs, password hashes, bootstrap state and all application rows. Unused copied databases and empty Word add-in directories were archived outside the project. The Word integration has no active routes, schema or frontend; standard DOCX exports remain. All 71 focused migration/authentication/API/retrieval/export checks passed, including real PDF conversion. See [the database audit](database-cleanup.md).
