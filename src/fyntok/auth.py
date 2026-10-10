"""ChatGPT OAuth. Secrets stay in an owner-only store outside the database."""

import base64
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, TypedDict, cast, override
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

import httpx
import jwt

RESOURCE = "https://api.openai.com/v1"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"  # noqa: S105 - OAuth endpoint, no secret.
DISCOVERY = "https://auth.openai.com/.well-known/openid-configuration"
PERMISSION = "chatgpt.tokens.use.direct"


class PendingLogin(TypedDict):
    state: str
    nonce: str
    verifier: str
    redirect: str
    created: float
    client_id: str | None
    subject: str | None


class ChatGPTSession:
    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = Path(directory or Path.home() / ".config" / "fyntok")
        self.path = self.directory / "accounts.json"
        self.pending: PendingLogin | None = None
        self.login_error: str | None = None

    @contextmanager
    def locked(self) -> Generator[None]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        with (self.directory / "session.lock").open("a") as lock:
            Path(lock.name).chmod(0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def load(self) -> dict[str, Any]:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"host_id": "urn:uuid:" + str(uuid4()), "accounts": {}, "active": None}

    def save(self, record: dict[str, Any]) -> None:
        fd, name = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(record, out)
            Path(name).replace(self.path)
        finally:
            if Path(name).exists():
                Path(name).unlink()

    def accounts(self) -> dict[str, str]:
        with self.locked():
            data = self.load()
            return {
                cid: a.get("email", a["subject"]) + " · " + cid[-8:]
                for cid, a in data["accounts"].items()
            }

    def active_account(self) -> str | None:
        with self.locked():
            data = self.load()
            return cast("str | None", data["active"])

    def select(self, client_id: str | None) -> None:
        with self.locked():
            data = self.load()
            if client_id not in data["accounts"]:
                raise ValueError("Unknown ChatGPT registration.")
            data["active"] = client_id
            self.save(data)

    def begin(self, redirect_uri: str, client_id: str | None = None) -> str:
        with self.locked():
            if self.pending:
                raise ValueError("A sign-in is already in progress.")
            data = self.load()
            retry = cast("str | None", data.get("retry_client_id"))
            client_id = client_id or retry
            prior = data["accounts"].get(client_id, {})
            if client_id is not None and client_id != "" and not prior and client_id != retry:
                raise ValueError("Unknown ChatGPT registration.")
            self.save(data)  # Persist host before the first authorization.
            p: PendingLogin = {
                "state": secrets.token_urlsafe(32),
                "nonce": secrets.token_urlsafe(32),
                "verifier": secrets.token_urlsafe(48),
                "redirect": redirect_uri,
                "created": time.time(),
                "client_id": client_id,
                "subject": prior.get("subject"),
            }
            self.pending = p
            self.login_error = None
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(p["verifier"].encode()).digest())
                .decode()
                .rstrip("=")
            )
            params = {
                "client_id": client_id or "dynamic_agent_client",
                "ext_agent_host_id": data["host_id"],
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": "openid profile email offline_access resource.invoke " + PERMISSION,
                "resource": RESOURCE,
                "state": p["state"],
                "nonce": p["nonce"],
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
            if not (client_id is not None and client_id != ""):
                params["agent_name_hint"] = "fyntok"
            # Omit ID-token hints so no tokens are exposed in browser-rendered URLs.
            return "https://auth.openai.com/api/accounts/authorize?" + urlencode(params)

    def complete(self, callback: str) -> None:
        with self.locked():
            p = self.pending
            if not p:
                raise ValueError("No pending sign-in.")
            parsed = urlparse(callback)
            if parsed._replace(query="", fragment="").geturl() != p["redirect"]:
                raise ValueError("Unexpected callback URI.")
            query = parse_qs(parsed.query, keep_blank_values=True)
            if any(len(v) != 1 for v in query.values()):
                raise ValueError("Duplicate callback parameters.")

            def value(k: str) -> str:
                return query.get(k, [""])[0]

            if not secrets.compare_digest(value("state"), p["state"]):
                raise ValueError("OAuth state mismatch.")
            self.pending = None  # Single-use attempt, including denied consent.
            if time.time() - p["created"] > 900 or value("error"):
                raise ValueError("Sign-in expired, declined, or failed. Try again.")
            cid = value("client_id") or p["client_id"]
            if (
                not (cid is not None and cid != "")
                or cid == "dynamic_agent_client"
                or (p["client_id"] is not None and p["client_id"] != "" and cid != p["client_id"])
            ):
                raise ValueError("Invalid issued client ID.")
            if not value("code"):
                raise ValueError("Missing authorization code.")
            if not (p["client_id"] is not None and p["client_id"] != "") or cid == self.load().get(
                "retry_client_id"
            ):
                registration = self.load()
                registration["retry_client_id"] = cid
                self.save(registration)
            with httpx.Client(timeout=30) as client:
                response = client.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": cid,
                        "code": value("code"),
                        "code_verifier": p["verifier"],
                        "redirect_uri": p["redirect"],
                        "resource": RESOURCE,
                    },
                )
                if response.is_error:
                    raise RuntimeError(
                        f"Sign-in exchange failed (HTTP {response.status_code}). Try again."
                    )
                tokens = response.json()
                metadata_response = client.get(DISCOVERY)
                metadata_response.raise_for_status()
                metadata = metadata_response.json()
            if metadata["issuer"] != "https://auth.openai.com" or not metadata[
                "jwks_uri"
            ].startswith("https://auth.openai.com/"):
                raise ValueError("Unexpected OpenID provider.")
            key = jwt.PyJWKClient(metadata["jwks_uri"]).get_signing_key_from_jwt(tokens["id_token"])
            claims = jwt.decode(
                tokens["id_token"],
                key.key,
                algorithms=["RS256"],
                audience=cid,
                issuer=metadata["issuer"],
                options={"require": ["exp", "iss", "aud", "sub", "nonce"]},
            )
            if not secrets.compare_digest(claims["nonce"], p["nonce"]) or (
                p["subject"] is not None and p["subject"] != "" and claims["sub"] != p["subject"]
            ):
                raise ValueError("Sign-in identity or nonce mismatch.")
            data = self.load()
            prior = data["accounts"].get(cid)
            if prior and prior["subject"] != claims["sub"]:
                raise ValueError("Registration belongs to another account.")
            data["accounts"][cid] = {
                **tokens,
                "client_id": cid,
                "subject": claims["sub"],
                "email": claims.get("email", claims["sub"]),
                "scopes": tokens.get("scope", "").split(),
                "expires_at": time.time() + tokens.get("expires_in", 3600),
            }
            data["active"] = cid
            if data.get("retry_client_id") == cid:
                data.pop("retry_client_id")
            self.save(data)

    def access_token(self) -> str:
        with self.locked():
            data = self.load()
            record = data["accounts"].get(data["active"], {})
            if not record.get("access_token"):
                raise RuntimeError(
                    "Continue with ChatGPT to enable AI. Manual review remains available."
                )
            if PERMISSION not in record.get("scopes", []):
                raise RuntimeError(
                    "ChatGPT plan usage was not granted. Sign in with plan permission."
                )
            if record["expires_at"] < time.time() + 60:
                if not record.get("refresh_token"):
                    raise RuntimeError("Session expired. Sign in again.")
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
                    try:
                        error = response.json().get("error")
                        code = error.get("code") if isinstance(error, dict) else error
                    except ValueError, AttributeError:
                        code = None
                    if code in {
                        "invalid_grant",
                        "invalid_refresh_token",
                        "token_expired",
                        "refresh_token_expired",
                        "refresh_token_invalidated",
                        "refresh_token_reused",
                    }:
                        for field in ("access_token", "refresh_token", "id_token"):
                            record.pop(field, None)
                        self.save(data)
                        raise RuntimeError(
                            "Session expired or revoked. Sign in again using the saved account."
                        )
                    raise RuntimeError(
                        f"Session refresh failed (HTTP {response.status_code}). "
                        f"Credentials retained; retry later or check client configuration."
                    )
                tokens = response.json()
                record.update(tokens)
                if "scope" in tokens:
                    record["scopes"] = tokens["scope"].split()
                record["expires_at"] = time.time() + tokens.get("expires_in", 3600)
                self.save(data)
                if PERMISSION not in record["scopes"]:
                    raise RuntimeError("ChatGPT plan permission is no longer granted.")
            return str(record["access_token"])

    def sign_out(self) -> bool:
        """Clear local secrets even when remote revocation cannot be confirmed."""
        with self.locked():
            self.pending = None
            data = self.load()
            record = data["accounts"].get(data["active"], {})
            confirmed = not record.get("refresh_token")
            try:
                if record.get("refresh_token"):
                    with httpx.Client(timeout=15) as client:
                        metadata = client.get(DISCOVERY).json()
                        endpoint = metadata["revocation_endpoint"]
                        if not endpoint.startswith("https://auth.openai.com/"):
                            raise ValueError("Unexpected revocation endpoint.")
                        response = client.post(
                            endpoint,
                            data={
                                "token": record["refresh_token"],
                                "token_type_hint": "refresh_token",
                                "client_id": record["client_id"],
                            },
                        )
                        confirmed = response.status_code == 200
            except httpx.HTTPError, ValueError, KeyError:
                confirmed = False
            for field in ("access_token", "refresh_token", "id_token"):
                record.pop(field, None)
            self.save(data)
            return confirmed

    def start_login(self, client_id: str | None = None) -> str:
        owner = self

        class Callback(BaseHTTPRequestHandler):
            @override
            def log_message(self, format: str, *args: object) -> None:
                pass  # Callback URLs contain authorization codes.

            def do_GET(self) -> None:
                if urlparse(self.path).path != "/auth/callback":
                    self.send_error(404)
                    return
                try:
                    owner.complete(redirect + "?" + urlparse(self.path).query)
                    message, status = "Sign-in complete. Return to fyntok to use AI.", 200
                except Exception:
                    owner.login_error = "Sign-in failed. Please try again."
                    message, status = owner.login_error, 400
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(message.encode())

        server = HTTPServer(("127.0.0.1", 0), Callback)
        redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
        try:
            url = self.begin(redirect, client_id)
        except Exception:
            server.server_close()
            raise

        attempt = self.pending

        def listen() -> None:
            with server:
                server.timeout = 1
                deadline = time.time() + 900
                while owner.pending is attempt and time.time() < deadline:
                    server.handle_request()
            if owner.pending is attempt:
                owner.pending = None

        Thread(target=listen, daemon=True).start()
        return url
