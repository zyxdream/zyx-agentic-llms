# Working in zyx-agentic-llms

Work only in this repository. Do not modify the original source project, push, deploy, or contact paid providers unless the user authorizes that action.

The application is Workspace. Preserve the Flask, Redis, Celery, SQLAlchemy and Docker execution design. The only environment is `dev-monica`; use the public shared configuration and ignored root `.env`. Never print credentials or fixture contents to external services.

Run backend checks with `ZYX_E2E=1 python -m pytest -q -p tests.offline_guard` using this repository's `.venv/bin/python`. Never run against another project's environment, database, Redis or user pool. Local services are frontend 3010, API 5010 and Redis 6390; `scripts/dev.py` controls only owned process groups. Run frontend commands from `frontend/`: `CI=true npm test -- --watch=false --runInBand` and `npm run build`. Docker checks are opt-in with `ZYX_PYTHON_LIVE=1`. Full Redis/Celery replay uses `ZYX_REPLAY_E2E=1`; its fixture starts an owned Redis process. Run browser replays with `npm run test:e2e` from `frontend/`. Keep production authentication separate from the test-only browser entry. See `tests/e2e/README.md` for the complete mocked workflow.

Use only the independent fresh-database baseline. Never upgrade, rewrite or delete an older database or source collection. SQL run state owns progress; Redis task results are not the application record. Preserve lease fencing, ownership, source-version validation, idempotent submissions and atomic document/turn publication.

See `docs/architecture.md` and `docs/refactoring-status.md` before changing module boundaries or broadening scope. Agent-loop behavior is described in `docs/agent-harness.md`; installation and operation are in `docs/runbook.md`; live suites and their recorded baselines are in `tests/live/README.md`.
