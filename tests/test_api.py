import os
from fastapi.testclient import TestClient

from api.app.config import get_settings
from api.app.main import app


class DummyResp:
    def __init__(self):
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self._json = {"ok": True}
        self.content = b'{"ok": true}'

    def json(self):
        return self._json


async def _fake_proxy_call(*args, **kwargs):
    return DummyResp()


def test_proxy_endpoint_monkeypatch() -> None:
    os.environ["CLOUDFLARE_WORKER_BASE_URLS"] = "https://worker.example.test"
    get_settings.cache_clear()

    with TestClient(app) as client:
        app.state.services.gemini.proxy_call = _fake_proxy_call
        resp = client.post(
            "/proxy/gemini",
            json={"model": "gemini-2.5-flash", "contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
