# Dependency updates

This repository does not accept automatic dependency-version pull requests.
Updates are applied in planned batches, in compatible groups, and only after the
full local verification gate passes.

## Policy

- **No Dependabot version-update PRs.** The previous `.github/dependabot.yml`
  opened one PR per package and was removed. Closed bot PRs are not merged.
- **Quarterly planned updates.** One branch and one review per batch, not one PR
  per package.
- **Alerts only.** Dependabot alerts may be enabled where the repository plan
  supports them. Automatic security-update PRs stay off. There is no separate
  scheduled audit job; security review happens during the quarterly window and
  when GitHub reports an alert.
- **No drive-by upgrades.** A package is updated because it is needed or because
  it belongs to the current quarterly batch, not because a newer version exists.
- **Compatibility before version.** A newer release is rejected if it breaks the
  supported interpreter, the legacy document parser, the worker stack or the
  Docker runtime.

## Compatibility contract

| Component | Supported value |
| --- | --- |
| Host Python | **3.11.x** (3.11.18 is the current verified local build) |
| CI Python | **3.11** |
| Node | 22.x |
| Installer | `pip < 24.1` (textract 1.6.x metadata) |
| Redis | 7.x, local port 6390 |
| Docker runtime | Debian bookworm image, Python 3.11 |
| Frontend install | `npm ci --legacy-peer-deps` |

Python 3.12 and newer are not part of the current contract. They can be
evaluated as a separate interpreter migration after the 3.11 baseline has
completed a quarterly cycle.

## Quarterly procedure

1. Create a branch, for example `deps/2027-q1`.
2. Choose one group from the table below. Do not mix unrelated groups.
3. Check the candidate release notes and `requires_python` before editing pins.
4. Update the pins in `requirements.txt`, or the frontend dependencies in
   `frontend/package.json` and `package-lock.json`.
5. Recreate the environment and run the gate.
6. Record the result in the batch commit message: updated packages, test counts,
   and any package deliberately held back.
7. Merge only after the gate passes. Revert the group if it fails; do not upgrade
   additional packages to compensate.

### Backend gate

```sh
rm -rf .venv
"$(brew --prefix python@3.11)/bin/python3.11" -m venv .venv
.venv/bin/python -m pip install --upgrade 'pip<24.1'
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check

ZYX_E2E=1 .venv/bin/python -m pytest -q -p tests.offline_guard
ZYX_E2E=1 ZYX_REPLAY_E2E=1 ZYX_PYTHON_LIVE=1 \
  .venv/bin/python -m pytest -q -p tests.offline_guard
docker build -t zyx-agentic-llms-python-analysis:1 backend/python_runtime
```

The offline suite uses mocked providers and temporary databases. The replay suite
uses its own Redis process, containers and storage; it never points at the local
development database or queues.

### Frontend gate

```sh
npm --prefix frontend ci --legacy-peer-deps
cd frontend
CI=true npm test -- --watch=false --runInBand
npm run build
npm run test:e2e
```

### Document-parser check

The highest-risk area is the legacy parser stack. After changing `textract`,
`pdfminer.six`, `beautifulsoup4`, `soupsieve`, `python-docx`, `openpyxl`,
`PyMuPDF` or `pillow`, exercise real `.pdf`, `.doc`, `.docx`, `.xlsx` and `.msg`
inputs through preparation and retrieval. A successful install is not proof that
extraction still works.

## Update groups

Update a whole group together. Several of these packages are coupled by hard
version constraints.

| Group | Packages | Notes |
| --- | --- | --- |
| Legacy document parser | `textract`, `pdfminer.six`, `beautifulsoup4`, `soupsieve`, `chardet`, `docx2txt`, `extract-msg`, `xlrd`, `six` | `textract 1.6.5` pins this set. Evaluate `textract 2.x` as its own migration. Never bump `soupsieve` alone against the old BeautifulSoup pin. |
| Data and workbooks | `pandas`, `numpy`, `fsspec`, `openpyxl`, `XlsxWriter`, `python-dateutil`, `pytz`, `tzdata` | `pandas 3.x` requires Python ≥ 3.11 and may change dtype/IO behavior. |
| Flask and persistence | `Flask`, `Werkzeug`, `Jinja2`, `MarkupSafe`, `itsdangerous`, `click`, `Flask-*`, `SQLAlchemy`, `Alembic` | Keep the Flask extension set and SQLAlchemy compatible. |
| Celery and queue | `celery`, `kombu`, `billiard`, `vine`, `amqp`, `redis` | Requires the real Redis/Celery replay. |
| Provider and HTTP | `openai`, `httpx`, `httpcore`, `anyio`, `h11`, `sniffio`, `certifi`, `requests`, `urllib3` | Provider transport changes need a live smoke request when authorized. |
| Validation and typing | `pydantic`, `pydantic-core`, `annotated-types`, `typing_extensions` | Keep the compiled core aligned with pydantic. |
| PDF and imaging | `PyMuPDF`, `PyMuPDFb`, `pillow`, `reportlab`, `fonttools` | Check wheels for the supported Python and both macOS architectures used by the team. |
| Tokenization | `tiktoken`, `regex` | Token estimates and context-budget tests must remain stable. |
| Test tooling | `pytest`, `pytest-cov`, `pluggy`, `iniconfig`, `packaging`, `coverage` | No application behavior change expected. |
| Frontend | React group, `marked`, `axios`, `@playwright/test`, Jest/testing-library | Run component tests, production build and browser replay. |
| GitHub Actions | `actions/checkout`, `setup-python`, `setup-node`, `upload-artifact` | Review major-version runtime changes; keep the workflow on Node 22. |

## Known ceilings

| Package | Current pin | Constraint |
| --- | --- | --- |
| `pandas` | 2.3.1 | 3.x requires Python ≥ 3.11; adopt after the interpreter migration and a data/workbook regression pass. |
| `soupsieve` | 2.9 | 4.x requires Python ≥ 3.11.5 and must move with `beautifulsoup4`, not alone. |
| `textract` | 1.6.5 | Non-standard `extract-msg<=0.29.*` metadata; requires `pip < 24.1`. 2.x is a separate migration. |
| `pdfminer.six` | 20191110 | Pinned by textract 1.6.5; do not move independently. |
| `beautifulsoup4` | 4.8.2 | Pinned by textract; upgrade with `soupsieve`. |
| `boto3` | `>=1.34.0` | Unpinned. Pin it in the next quarterly batch if reproducible builds matter. |

## Recording a batch

The commit message should name the group and the gate result, for example:

```text
Update the Celery queue group

celery 5.5.3 -> 5.7.x, kombu/billiard/vine/amqp aligned.
Offline: 966 passed, 20 skipped, 0 failures.
Replay: passed. Docker: passed. Frontend: unchanged.
```

Do not rewrite the previous batch's report. If a batch is reverted, keep the
reverted commit in history and record why.
