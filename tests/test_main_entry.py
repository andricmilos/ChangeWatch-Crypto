import pytest

import app.__main__ as entry
from app.config import Settings


@pytest.mark.parametrize(("app_env", "reload"), [("development", True), ("production", False)])
def test_main_passes_settings_to_uvicorn(monkeypatch, app_env, reload):
    settings = Settings(_env_file=None, app_env=app_env, host="0.0.0.0", port=9123)
    calls = []
    monkeypatch.setattr(entry, "get_settings", lambda: settings)
    # Replaces the real server start, so the test never binds a port.
    monkeypatch.setattr(entry.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    entry.main()

    assert calls == [(("app.main:app",), {"host": "0.0.0.0", "port": 9123, "reload": reload})]
