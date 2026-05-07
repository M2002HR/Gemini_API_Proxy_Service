from api.app.config import Settings


def test_settings_defaults() -> None:
    s = Settings()
    assert s.proxy.mode in {"cloudflare_worker", "gemini_direct"}
    assert s.gemini.default_model
