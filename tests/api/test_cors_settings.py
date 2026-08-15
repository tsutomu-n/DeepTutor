"""Tests for FastAPI CORS settings."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
import pytest

from deeptutor.api import main as api_main


def test_cors_allows_remote_http_origins_when_auth_disabled(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.delenv("CORS_ORIGIN", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.setenv("FRONTEND_PORT", "3782")

    settings = api_main._build_cors_settings()

    assert settings["allow_origin_regex"] == r"https?://.*"
    assert "http://localhost:3782" in settings["allow_origins"]
    assert "http://127.0.0.1:3782" in settings["allow_origins"]


def test_cors_requires_explicit_origins_when_auth_enabled(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("CORS_ORIGIN", "https://app.example.com/")
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "https://foo.example.com, https://bar.example.com\nhttps://foo.example.com",
    )

    settings = api_main._build_cors_settings()

    assert settings["allow_origin_regex"] is None
    assert "https://app.example.com" in settings["allow_origins"]
    assert "https://foo.example.com" in settings["allow_origins"]
    assert "https://bar.example.com" in settings["allow_origins"]
    assert settings["allow_origins"].count("https://foo.example.com") == 1


@pytest.mark.parametrize("unsafe_origin", ["*", "null"])
def test_cors_rejects_credentialed_wildcard_or_opaque_origin(
    monkeypatch,
    unsafe_origin: str,
) -> None:
    monkeypatch.setattr(api_main, "load_auth_settings", lambda: {"enabled": True})
    monkeypatch.setattr(
        api_main,
        "load_system_settings",
        lambda: {
            "frontend_port": 3782,
            "cors_origin": unsafe_origin,
            "cors_origins": ["https://learn.example.com"],
        },
    )

    with pytest.raises(ValueError, match="credentialed CORS"):
        api_main._build_cors_settings()


def test_auth_enabled_cors_middleware_does_not_echo_unlisted_cookie_origin(
    monkeypatch,
) -> None:
    allowed_origin = "https://learn.example.com"
    monkeypatch.setattr(api_main, "load_auth_settings", lambda: {"enabled": True})
    monkeypatch.setattr(
        api_main,
        "load_system_settings",
        lambda: {
            "frontend_port": 3782,
            "cors_origin": allowed_origin,
            "cors_origins": [],
        },
    )
    settings = api_main._build_cors_settings()
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings["allow_origins"],
        allow_origin_regex=settings["allow_origin_regex"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/write")
    def write() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    evil = client.post(
        "/write",
        json={"value": 1},
        cookies={"dt_token": "secret"},
        headers={"Origin": "https://evil.example.com"},
    )
    preflight = client.options(
        "/write",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    allowed = client.post(
        "/write",
        json={"value": 1},
        cookies={"dt_token": "secret"},
        headers={"Origin": allowed_origin},
    )

    assert "access-control-allow-origin" not in evil.headers
    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in preflight.headers
    assert allowed.headers["access-control-allow-origin"] == allowed_origin


def test_cors_normalizes_common_origin_input_mistakes(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv(
        "CORS_ORIGIN",
        "172.26.0.10:3782; https://learn.example.com/app/",
    )
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000;api.example.com")

    settings = api_main._build_cors_settings()

    assert settings["allow_origin_regex"] is None
    assert "http://172.26.0.10:3782" in settings["allow_origins"]
    assert "https://learn.example.com" in settings["allow_origins"]
    assert "http://api.example.com" in settings["allow_origins"]


def test_cors_preflight_allows_partner_patch_save() -> None:
    client = TestClient(api_main.app)

    response = client.options(
        "/api/v1/partners/partner",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    allowed_methods = {
        method.strip() for method in response.headers["access-control-allow-methods"].split(",")
    }
    assert "PATCH" in allowed_methods
