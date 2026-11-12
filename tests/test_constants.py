import importlib


def test_environment_loaded_from_env(monkeypatch):
    import backend.constants as constants

    monkeypatch.setenv("ENVIRONMENT", "dev-peter")
    reloaded = importlib.reload(constants)

    assert reloaded.ENVIRONMENT == "dev-peter"
    assert reloaded.API_URL == "http://127.0.0.1:5000/"

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    importlib.reload(constants)
