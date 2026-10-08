"""Minimal SIWC experiment login; credentials live outside this repository."""

import argparse
import base64
import getpass
import hashlib
import json
import os
import secrets
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, override
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt

CONFIG = Path(os.environ.get("FYNTOK_CREDENTIAL_DIR", str(Path.home() / ".fyntok-experiment")))
CREDENTIALS = CONFIG / "credentials.json"
PENDING = CONFIG / "pending.json"
RESOURCE = "https://api.openai.com/v1"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"  # noqa: S105 - OAuth endpoint, no secret.
REDIRECT = "http://127.0.0.1:1456/auth/callback"


def save_private(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out)
        Path(name).replace(path)
    finally:
        if Path(name).exists():
            Path(name).unlink()


def begin() -> str:
    host_file = CONFIG / "host.json"
    if not host_file.exists():
        save_private(host_file, {"ext_agent_host_id": "urn:uuid:" + str(uuid.uuid4())})
    host = json.loads(host_file.read_text())
    prior = json.loads(CREDENTIALS.read_text()) if CREDENTIALS.exists() else {}
    pending = {
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "code_verifier": secrets.token_urlsafe(48),
        "redirect_uri": REDIRECT,
        "created_at": time.time(),
        "client_id": prior.get("client_id"),
        "subject": prior.get("subject"),
        **host,
    }
    save_private(PENDING, pending)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(pending["code_verifier"].encode()).digest())
        .decode()
        .rstrip("=")
    )
    params = {
        "client_id": pending["client_id"] or "dynamic_agent_client",
        "ext_agent_host_id": host["ext_agent_host_id"],
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
        "resource": RESOURCE,
        "state": pending["state"],
        "nonce": pending["nonce"],
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    if not pending["client_id"]:
        params["agent_name_hint"] = "fyntok categorization experiment"
    # No ID-token hints in printed URLs.
    return "https://auth.openai.com/api/accounts/authorize?" + urlencode(params)


def complete(callback: str) -> None:
    pending = json.loads(PENDING.read_text())
    parsed = urlparse(callback)
    if (parsed.scheme, parsed.netloc, parsed.path) != ("http", "127.0.0.1:1456", "/auth/callback"):
        raise ValueError("Unexpected callback URI")
    query = parse_qs(parsed.query)
    if any(len(values) != 1 for values in query.values()):
        raise ValueError("Duplicate callback parameters")

    def value(key: str) -> str:
        return query.get(key, [""])[0]

    if not secrets.compare_digest(value("state"), pending["state"]):
        raise ValueError("OAuth state mismatch")
    if time.time() - pending["created_at"] > 900:
        raise ValueError("Sign-in attempt expired; start again")
    if value("error"):
        raise ValueError("Sign-in declined or failed")
    client_id = value("client_id") or pending["client_id"]
    if not client_id or client_id == "dynamic_agent_client":
        raise ValueError("Missing issued client ID")
    if pending["client_id"] and pending["client_id"] != client_id:
        raise ValueError("Issued client ID changed")
    if not value("code"):
        raise ValueError("Missing authorization code")
    # Retain registration if the single-use code exchange fails.
    pending["client_id"] = client_id
    save_private(PENDING, pending)
    with httpx.Client(timeout=30) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": value("code"),
                "code_verifier": pending["code_verifier"],
                "redirect_uri": pending["redirect_uri"],
                "resource": RESOURCE,
            },
        )
        if response.is_error:
            raise RuntimeError(f"Token exchange failed: HTTP {response.status_code}")
        tokens = response.json()
        discovery = client.get("https://auth.openai.com/.well-known/openid-configuration")
        discovery.raise_for_status()
        metadata = discovery.json()
    if metadata["issuer"] != "https://auth.openai.com":
        raise ValueError("Unexpected OIDC issuer")
    key = jwt.PyJWKClient(metadata["jwks_uri"]).get_signing_key_from_jwt(tokens["id_token"])
    claims = jwt.decode(
        tokens["id_token"],
        key.key,
        algorithms=["RS256"],
        audience=client_id,
        issuer=metadata["issuer"],
        options={"require": ["exp", "iss", "aud", "sub", "nonce"]},
    )
    if not secrets.compare_digest(claims["nonce"], pending["nonce"]):
        raise ValueError("OIDC nonce mismatch")
    if pending["subject"] and claims["sub"] != pending["subject"]:
        raise ValueError("Signed-in account changed")
    scopes = tokens.get("scope", "").split()
    record = {
        **tokens,
        "scopes": scopes,
        "subject": claims["sub"],
        "client_id": client_id,
        "ext_agent_host_id": pending["ext_agent_host_id"],
        "expires_at": time.time() + tokens.get("expires_in", 3600),
    }
    save_private(CREDENTIALS, record)
    PENDING.unlink()
    if "chatgpt.tokens.use.direct" not in scopes:
        raise ValueError("Signed in, but ChatGPT plan usage was not granted")
    print("ChatGPT plan permission granted; credentials saved outside the repository.")


def access_token() -> str:
    if not CREDENTIALS.exists():
        raise RuntimeError("Run uv run auth.py serve and complete ChatGPT sign-in first")
    record = json.loads(CREDENTIALS.read_text())
    if "chatgpt.tokens.use.direct" not in record.get("scopes", []):
        raise RuntimeError("ChatGPT plan usage permission is missing")
    if record["expires_at"] < time.time() + 60:
        response = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": record["client_id"],
                "refresh_token": record["refresh_token"],
                "resource": RESOURCE,
            },
            timeout=30,
        )
        if response.is_error:
            raise RuntimeError(f"Refresh failed: HTTP {response.status_code}; sign in again")
        tokens = response.json()
        record.update(tokens)
        if "scope" in tokens:
            record["scopes"] = tokens["scope"].split()
        record["expires_at"] = time.time() + tokens.get("expires_in", 3600)
        save_private(CREDENTIALS, record)
        if "chatgpt.tokens.use.direct" not in record["scopes"]:
            raise RuntimeError("ChatGPT plan permission no longer granted")
    return str(record["access_token"])


def serve() -> None:
    class Callback(BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: object) -> None:
            pass  # Never log callback authorization codes.

        def do_GET(self) -> None:
            if urlparse(self.path).path != "/auth/callback":
                self.send_error(404)
                return
            try:
                complete("http://127.0.0.1:1456" + self.path)
                message = "Sign-in complete. You can close this tab."
                status = 200
            except Exception as exc:
                message = f"Sign-in failed ({type(exc).__name__}). Check the experiment terminal."
                status = 400
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(message.encode())

    with HTTPServer(("127.0.0.1", 1456), Callback) as server:
        server.timeout = 1
        print(begin(), flush=True)
        deadline = time.time() + 900
        while PENDING.exists() and time.time() < deadline:
            server.handle_request()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["serve", "complete"])
    args = parser.parse_args()
    if args.command == "serve":
        serve()
    else:
        complete(getpass.getpass("Paste the complete redirected loopback URL (hidden): "))
