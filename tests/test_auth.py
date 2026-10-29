"""Credential lifecycle checks use local mock OAuth responses, never real credentials."""

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from fyntok.auth import PERMISSION, RESOURCE, ChatGPTSession


def record(session: ChatGPTSession) -> None:
    with session.locked():
        data = session.load()
        data["accounts"]["issued-client"] = {
            "client_id": "issued-client",
            "subject": "person",
            "email": "person@example.com",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "id_token": "old-id",
            "scopes": [PERMISSION],
            "expires_at": time.time() - 1,
        }
        data["active"] = "issued-client"
        session.save(data)


def test_host_and_registration_survive_signout_without_retaining_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = ChatGPTSession(tmp_path / "credentials")
    first = parse_qs(urlparse(session.begin("http://127.0.0.1:1456/auth/callback")).query)
    assert first["client_id"] == ["dynamic_agent_client"]
    assert first["agent_name_hint"] == ["fyntok"]
    session.pending = None
    record(session)
    monkeypatch.setattr(
        httpx.Client,
        "get",
        lambda *_a, **_k: httpx.Response(
            200, json={"revocation_endpoint": "https://auth.openai.com/revoke"}
        ),
    )
    monkeypatch.setattr(httpx.Client, "post", lambda *_a, **_k: httpx.Response(200))
    assert session.sign_out()
    saved = json.loads(session.path.read_text())
    assert (
        not {"access_token", "refresh_token", "id_token"}
        & saved["accounts"]["issued-client"].keys()
    )
    assert saved["accounts"]["issued-client"]["subject"] == "person"
    restarted = ChatGPTSession(session.directory)
    returning = parse_qs(
        urlparse(restarted.begin("http://127.0.0.1:1457/auth/callback", "issued-client")).query
    )
    assert returning["client_id"] == ["issued-client"]
    assert returning["ext_agent_host_id"] == first["ext_agent_host_id"]
    assert returning["state"] != first["state"]
    assert "agent_name_hint" not in returning
    assert session.path.stat().st_mode & 0o777 == 0o600
    assert session.directory.stat().st_mode & 0o777 == 0o700


def test_refresh_rotates_tokens_persistently_and_keeps_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = ChatGPTSession(tmp_path)
    record(session)

    def refresh(_url: str, data: dict[str, Any], **_kwargs: object) -> httpx.Response:
        assert data["client_id"] == "issued-client" and data["resource"] == RESOURCE
        assert data["refresh_token"] == "old-refresh" and "scope" not in data
        return httpx.Response(
            200,
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600},
        )

    monkeypatch.setattr(httpx, "post", refresh)
    assert session.access_token() == "new-access"
    assert ChatGPTSession(tmp_path).access_token() == "new-access"
    assert (
        json.loads(session.path.read_text())["accounts"]["issued-client"]["refresh_token"]
        == "new-refresh"
    )


def test_missing_plan_permission_never_reaches_inference(tmp_path: Path) -> None:
    session = ChatGPTSession(tmp_path)
    record(session)
    with session.locked():
        data = session.load()
        data["accounts"]["issued-client"]["scopes"] = ["openid"]
        session.save(data)
    with pytest.raises(RuntimeError, match="not granted"):
        session.access_token()


def test_callback_checks_state_and_rejects_changed_client_before_exchange(tmp_path: Path) -> None:
    session = ChatGPTSession(tmp_path)
    record(session)
    session.begin("http://127.0.0.1:1456/auth/callback", "issued-client")
    with pytest.raises(ValueError, match="state"):
        session.complete("http://127.0.0.1:1456/auth/callback?state=wrong&code=x")
    assert session.pending is not None
    state = session.pending["state"]
    with pytest.raises(ValueError, match="client"):
        session.complete(
            f"http://127.0.0.1:1456/auth/callback?state={state}&code=x&client_id=other"
        )


def test_declined_chatgpt_login_reports_failure_without_connecting(tmp_path: Path) -> None:
    session = ChatGPTSession(tmp_path)
    authorization = parse_qs(urlparse(session.start_login()).query)
    try:
        with httpx.Client(trust_env=False, timeout=5) as browser:
            response = browser.get(
                authorization["redirect_uri"][0],
                params={"state": authorization["state"][0], "error": "access_denied"},
            )
        assert response.status_code == 400
        assert response.text == "Sign-in failed. Please try again."
        assert session.login_error == response.text
        assert session.pending is None
        assert not session.is_connected(session.active_account())
    finally:
        session.pending = None


def test_local_signout_clears_secrets_when_revocation_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = ChatGPTSession(tmp_path)
    record(session)

    def offline(*_a: object, **_k: object) -> None:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx.Client, "get", offline)
    assert session.sign_out() is False
    with pytest.raises(RuntimeError, match="Continue with ChatGPT"):
        session.access_token()


@pytest.mark.parametrize(
    "problem",
    [
        pytest.param(None, id="successful-browser-login"),
        "nonce",
        "signature",
        "expired",
        "identity",
    ],
)
def test_code_exchange_validates_signed_identity_before_saving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str | None,
) -> None:
    from types import SimpleNamespace

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    session = ChatGPTSession(tmp_path)
    record(session)
    if problem is None:
        session.start_login("issued-client")
    else:
        session.begin("http://127.0.0.1:1456/auth/callback", "issued-client")
    assert session.pending is not None
    pending = session.pending.copy()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if problem == "signature"
        else key
    )
    claims = {
        "iss": "https://auth.openai.com",
        "aud": "issued-client",
        "sub": "different-person" if problem == "identity" else "person",
        "nonce": "wrong" if problem == "nonce" else pending["nonce"],
        "exp": time.time() - 60 if problem == "expired" else time.time() + 3600,
    }
    tokens = {
        "id_token": jwt.encode(claims, signer, algorithm="RS256"),
        "access_token": "signed-in-access",
        "refresh_token": "signed-in-refresh",
        "scope": PERMISSION,
        "expires_in": 3600,
    }

    def exchange(_self: httpx.Client, _url: str, data: dict[str, Any]) -> httpx.Response:
        assert data["client_id"] == "issued-client" and data["code_verifier"] == pending["verifier"]
        assert data["redirect_uri"] == pending["redirect"] and data["resource"] == RESOURCE
        return httpx.Response(200, json=tokens)

    monkeypatch.setattr(httpx.Client, "post", exchange)
    monkeypatch.setattr(
        httpx.Client,
        "get",
        lambda *_a, **_k: httpx.Response(
            200,
            json={"issuer": "https://auth.openai.com", "jwks_uri": "https://auth.openai.com/jwks"},
            request=httpx.Request(
                "GET", "https://auth.openai.com/.well-known/openid-configuration"
            ),
        ),
    )
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda *_a: SimpleNamespace(key=key.public_key()),
    )
    callback = f"{pending['redirect']}?state={pending['state']}&code=one-use-code"
    if problem is not None:
        with pytest.raises((ValueError, jwt.InvalidTokenError)):
            session.complete(callback)
        assert (
            json.loads(session.path.read_text())["accounts"]["issued-client"]["access_token"]
            == "old-access"
        )
    else:
        try:
            # Use request so the mocked discovery GET does not intercept the local browser.
            response = httpx.request("GET", callback, trust_env=False, timeout=5)
            assert response.status_code == 200
            assert response.text == "Sign-in complete. Return to fyntok to use AI."
            assert session.login_error is None
            assert session.pending is None
        finally:
            session.pending = None
        assert ChatGPTSession(tmp_path).access_token() == "signed-in-access"


@pytest.mark.parametrize("code", ["invalid_grant", "refresh_token_reused", "invalid_client", None])
def test_refresh_failure_clears_only_terminal_tokens_keeps_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str | None
) -> None:
    session = ChatGPTSession(tmp_path)
    record(session)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *_a, **_k: httpx.Response(
            400 if (code is not None and code != "") else 503,
            json={"error": code}
            if (code is not None and code != "")
            else {"detail": "Temporary failure"},
        ),
    )
    with pytest.raises(RuntimeError):
        session.access_token()
    saved = session.load()["accounts"]["issued-client"]
    assert saved["client_id"] == "issued-client" and saved["subject"] == "person"
    assert ("refresh_token" in saved) == (code not in {"invalid_grant", "refresh_token_reused"})
