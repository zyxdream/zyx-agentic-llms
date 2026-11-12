# AGENTS

## Python backend test environment

- The Python backend dependencies are installed in a Conda environment named `lexintel`.
- Before running backend tests or other Python tooling, activate that environment:

```bash
conda activate lexintel
```

- Example backend test command:

```bash
pytest tests
```

- If Python packages like `flask_sqlalchemy`, `flask_login`, or `pytest` appear to be missing, make sure the `lexintel` Conda environment is active first.
