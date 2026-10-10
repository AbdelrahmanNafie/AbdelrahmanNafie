import pytest

from jarvis import config
from jarvis.gateway import Gateway
from jarvis.store import Store


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_APPROVAL_WAIT_S", "2")
    monkeypatch.delenv("JARVIS_ALLOWED_DIRS", raising=False)
    monkeypatch.setenv("JARVIS_USER_DIRS", "0")
    (tmp_path / "code").mkdir()
    monkeypatch.setenv("JARVIS_CODE_DIRS", str(tmp_path / "code"))
    s = config.load()
    (s.home / "apps.json").write_text('{"Notepad": "notepad.exe"}')
    return config.load()


@pytest.fixture
def store(settings):
    return Store(settings.db_path)


@pytest.fixture
def launched():
    return []


@pytest.fixture
def gateway(settings, store, launched):
    return Gateway(settings, store, launcher=launched.append, poll_s=0.02)


@pytest.fixture(autouse=True)
def _fresh_model_preference():
    from jarvis import ears

    ears._preferred.clear()
    yield
    ears._preferred.clear()
