## LexText.ai

## Architecture and operating notes

- [Agent controller design memo and runbook](AGENT_CONTROLLER.md): active code
  reading order, old fixed workflow versus function calling, Celery continuation,
  context/memory and saved records, current rules, and remaining improvements.
- [Update and restart the normal local server](AGENT_CONTROLLER.md#update-and-restart-the-normal-local-server):
  exact Conda/environment setup, database backup/migration, Redis, Flask, Celery
  workers/Beat, frontend startup, and troubleshooting on this Mac.
- [Document preparation runbook](DOCUMENT_PREPARATION_RUNBOOK.md): upload
  preparation, source storage, workers, and the retained version-0 extraction path.
- [Spreadsheet Python runtime](backend/python_runtime/README.md): build the isolated
  analysis image, configure execution, and inspect the Python tool contract.

## Python environment

Use the `lexintel` Conda environment for backend commands. Run from the repository
root; if a tool additionally needs `PYTHONPATH`, point it at the current checkout:

```bash
export PYTHONPATH="$PWD"
```

## Database migrations

The migration repository is already initialized and committed. Do not run
`flask db init` in an existing checkout.

When a model change requires a schema migration, generate and review the
migration locally, then commit it with the model change:

```bash
flask --app backend.app db migrate -m "Describe the schema change"
flask --app backend.app db upgrade
```

On deployed servers, apply committed migrations without generating new ones:

```bash
flask --app backend.app db upgrade
```

To start a Flask shell:

```bash
flask --app backend.app shell
```
