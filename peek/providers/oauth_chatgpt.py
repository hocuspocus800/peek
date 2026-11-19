"""Sign in with ChatGPT (OpenAI's flow for local/open-source apps), per the docs at
developers.openai.com/siwc, read 2026-11-18.

- OAuth 2.0 authorization code + PKCE (S256), fresh `state` and OIDC `nonce` per
  attempt; endpoints from the discovery document (fallback: the documented URLs).
- Redirect to a loopback listener on 127.0.0.1 (never `localhost`); only the port
  may change between sign-ins. It accepts only GET /auth/callback with the right
  Host and state, and gives up after 10 minutes.
- Dynamic client registration: the first sign-in sends
  `client_id=dynamic_agent_client` + `agent_name_hint=Peek`; the issued client ID
  arrives on the redirect and is saved before the code is exchanged, then reused.
  One `ext_agent_host_id` (`urn:uuid:…`) per Mac, kept across sign-outs.
- Plan usage needs `chatgpt.tokens.use.direct` in the token response's `scope`;
  if the user declined it, Peek stays signed in but cannot answer, and says so.
- ID token verified with PyJWT against the JWKS: RS256, issuer, audience = the
  issued client ID, nonce, exp/iat/sub, 5 s leeway, `azp` if present.
- Access tokens last an hour; refresh tokens rotate on every use, so refreshes
  are serialized and the replacement is saved to the Keychain before the new
  access token is used. Terminal refresh errors sign out; network errors do not.
- Sign-out revokes the refresh token, clears the tokens, and keeps the issued
  client ID and host ID (the docs: signing out does not create a new client).

Secrets (tokens) live only in the Keychain; the account file holds the client
ID, host ID, email and `sub`. Nothing here logs a token or an authorize URL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from peek.providers.base import ProviderError

log = logging.getLogger(__name__)

ISSUER = "https://auth.openai.com"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
RESOURCE = "https://api.openai.com/v1"
FIRST_CLIENT_ID = "dynamic_agent_client"
AGENT_NAME = "Peek"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/auth/callback"
CALLBACK_PORTS = (1455, 1456, 1457, 1458, 1459)
SIGN_IN_TIMEOUT_S = 600.0
REFRESH_MARGIN_S = 60.0
USAGE_URL = "https://chatgpt.com/settings/usage"
TOKEN_ACCOUNT = "chatgpt_tokens"  # Keychain account (service: Peek)
TERMINAL_REFRESH_ERRORS = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}


@dataclass(frozen=True)
class Endpoints:
    authorize: str = f"{ISSUER}/api/accounts/authorize"
    token: str = f"{ISSUER}/api/accounts/oauth/token"
    revoke: str = f"{ISSUER}/api/accounts/oauth/revoke"
    jwks: str = f"{ISSUER}/.well-known/jwks.json"
    issuer: str = ISSUER


class SignInError(Exception):
    """A sign-in that did not complete; the message is safe to show."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # declined | cancelled | timeout | failed | other_account


# --- PKCE and the authorize URL ----------------------------------------------------


def make_pkce() -> tuple[str, str]:
    """(verifier, challenge): a 64-character verifier, its S256 challenge."""
    verifier = secrets.token_urlsafe(48)  # 64 URL-safe characters
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def new_host_id() -> str:
    return uuid.uuid4().urn  # "urn:uuid:…", a supported host-ID form


def authorize_url(
    endpoints: Endpoints,
    *,
    client_id: str | None,
    host_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    challenge: str,
    consent: bool = False,
    login_hint: str | None = None,
) -> str:
    """First sign-in (no client ID yet): dynamic registration with the app's name.
    Later: the issued client ID, optionally `prompt=consent` to ask for plan usage
    again after the user declined it."""
    params = {"client_id": client_id or FIRST_CLIENT_ID}
    if not client_id:
        params["agent_name_hint"] = AGENT_NAME
    params.update(
        {
            "ext_agent_host_id": host_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    if client_id and login_hint:
        params["login_hint"] = login_hint
    if consent:
        params["prompt"] = "consent"
    return f"{endpoints.authorize}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"


# --- the loopback listener -----------------------------------------------------------


class CallbackServer:
    """GET http://127.0.0.1:<port>/auth/callback, once, with the expected state."""

    def __init__(self, state: str, ports=CALLBACK_PORTS) -> None:
        self._state = state
        self._result: dict | None = None
        self._done = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 10  # an idle (pre-)connection must not block the real callback

            def do_GET(self):
                owner._handle(self)

            def log_message(self, *_args):  # the query holds the authorization code
                pass

        last_error: OSError | None = None
        for port in ports:
            try:
                self._server = ThreadingHTTPServer((CALLBACK_HOST, port), Handler)
                self._server.daemon_threads = True
                break
            except OSError as exc:
                last_error = exc
        else:
            raise SignInError("failed", "Couldn't listen for the sign-in (ports in use)") from (
                last_error
            )
        self.port = self._server.server_address[1]
        self.redirect_uri = f"http://{CALLBACK_HOST}:{self.port}{CALLBACK_PATH}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="chatgpt-callback", daemon=True
        )
        self._thread.start()

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlsplit(request.path)
        host = request.headers.get("Host", "")
        if parsed.path != CALLBACK_PATH or host != f"{CALLBACK_HOST}:{self.port}":
            request.send_response(404)
            request.end_headers()
            return
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if not hmac.compare_digest(query.get("state", ""), self._state):
            request.send_response(400)  # not our attempt: ignore it, keep waiting
            request.end_headers()
            return
        ok = "code" in query
        body = (
            "<!doctype html><meta charset=utf-8><title>Peek</title>"
            "<body style='font:15px -apple-system;margin:4em;text-align:center'>"
            + (
                "<h2>You're signed in to Peek</h2><p>You can close this tab.</p>"
                if ok
                else "<h2>Sign-in didn't finish</h2><p>You can close this tab and try again.</p>"
            )
        ).encode()
        request.send_response(200)
        request.send_header("Content-Type", "text/html; charset=utf-8")
        request.send_header("Cache-Control", "no-store")
        request.send_header("Referrer-Policy", "no-referrer")
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)
        if self._result is None:
            self._result = query
            self._done.set()

    def wait(self, timeout: float, cancel: threading.Event | None = None) -> dict:
        deadline = time.monotonic() + timeout
        while not self._done.wait(0.25):
            if cancel is not None and cancel.is_set():
                raise SignInError("cancelled", "Sign-in cancelled")
            if time.monotonic() > deadline:
                raise SignInError("timeout", "Sign-in timed out; try again")
        return dict(self._result or {})

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# --- tokens and the account ------------------------------------------------------------


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    id_token: str = ""
    expires_at: float = 0.0
    earliest_refresh_at: float = 0.0
    scopes: list[str] = field(default_factory=list)

    @property
    def plan_granted(self) -> bool:
        return PLAN_SCOPE in self.scopes

    @classmethod
    def from_response(cls, data: dict, now: float, previous: Tokens | None = None) -> Tokens:
        access = data.get("access_token")
        refresh = data.get("refresh_token") or (previous.refresh_token if previous else "")
        if not access or not refresh:
            raise ProviderError("failed", "OpenAI's sign-in reply was incomplete")
        scope = data.get("scope")
        scopes = scope.split() if isinstance(scope, str) else (previous.scopes if previous else [])
        earliest = data.get("earliest_refresh_at")
        return cls(
            access_token=access,
            refresh_token=refresh,
            id_token=data.get("id_token") or (previous.id_token if previous else ""),
            expires_at=now + float(data.get("expires_in") or 3600),
            earliest_refresh_at=float(earliest) if isinstance(earliest, int | float) else 0.0,
            scopes=scopes,
        )


@dataclass
class Account:
    """Not secret: kept in Application Support/Peek/chatgpt.json (0600)."""

    host_id: str = ""
    client_id: str = ""
    email: str = ""
    sub: str = ""
    plan_notice_shown: bool = False


def _default_account_path() -> Path:
    from peek.paths import support_dir

    return support_dir() / "chatgpt.json"


def _store_get(account: str) -> str | None:
    from peek import store

    return store.get_secret(account)


def _store_set(account: str, value: str) -> None:
    from peek import store

    store.set_secret(account, value)


def _store_delete(account: str) -> None:
    from peek import store

    store.delete_secret(account)


def _verify_id_token(
    id_token: str, *, client_id: str, nonce: str | None, endpoints: Endpoints, jwks_client
) -> dict:
    import jwt

    try:
        key = jwks_client.get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=endpoints.issuer,
            leeway=5,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except Exception as exc:
        log.warning("ID token rejected: %s", type(exc).__name__)
        raise SignInError("failed", "OpenAI's sign-in couldn't be verified") from None
    if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise SignInError("failed", "OpenAI's sign-in couldn't be verified (nonce)")
    if claims.get("azp") not in (None, client_id):
        raise SignInError("failed", "OpenAI's sign-in couldn't be verified (azp)")
    return claims


class ChatGPTSession:
    """The signed-in ChatGPT account. Thread-safe; blocking methods run off the main
    thread. Everything external is injectable for tests."""

    def __init__(
        self,
        *,
        http=None,
        secret_get: Callable[[str], str | None] = _store_get,
        secret_set: Callable[[str, str], None] = _store_set,
        secret_delete: Callable[[str], None] = _store_delete,
        account_path: Path | None = None,
        jwks_client=None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._get, self._set, self._delete = secret_get, secret_set, secret_delete
        self._account_path = account_path
        self._jwks_client = jwks_client
        self._clock = clock
        self._lock = threading.RLock()  # serializes refreshes and token writes
        self._tokens: Tokens | None = None
        self._tokens_loaded = False
        self._account: Account | None = None
        self._endpoints: Endpoints | None = None

    # --- plumbing -------------------------------------------------------------

    def _client(self):
        if self._http is None:
            import httpx2

            self._http = httpx2.Client(timeout=30.0, follow_redirects=False)
        return self._http

    def endpoints(self) -> Endpoints:
        """From the discovery document (cached); the documented URLs if it fails."""
        if self._endpoints is not None:
            return self._endpoints
        try:
            doc = self._client().get(DISCOVERY_URL).json()
            if doc.get("issuer") != ISSUER:
                raise ValueError("unexpected issuer")
            self._endpoints = Endpoints(
                authorize=doc["authorization_endpoint"],
                token=doc["token_endpoint"],
                revoke=doc.get("revocation_endpoint", Endpoints.revoke),
                jwks=doc["jwks_uri"],
                issuer=doc["issuer"],
            )
        except Exception as exc:
            log.warning("OpenAI discovery failed (%s); using the documented endpoints", exc)
            self._endpoints = Endpoints()
        return self._endpoints

    def _jwks(self, endpoints: Endpoints):
        if self._jwks_client is None:
            import jwt

            self._jwks_client = jwt.PyJWKClient(endpoints.jwks, timeout=15)
        return self._jwks_client

    def account(self) -> Account:
        with self._lock:
            if self._account is None:
                path = self._account_path or _default_account_path()
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    known = {k: v for k, v in raw.items() if k in Account.__dataclass_fields__}
                    self._account = Account(**known)
                except (OSError, ValueError, TypeError):
                    self._account = Account()
                if not self._account.host_id:
                    self._account.host_id = new_host_id()  # before the first sign-in
                    self._save_account()
            return self._account

    def _save_account(self) -> None:
        path = self._account_path or _default_account_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".chatgpt-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(asdict(self._account), f, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _load_tokens(self) -> Tokens | None:
        with self._lock:
            if not self._tokens_loaded:
                raw = self._get(TOKEN_ACCOUNT)
                try:
                    self._tokens = Tokens(**json.loads(raw)) if raw else None
                except (ValueError, TypeError):
                    log.warning("Stored ChatGPT tokens are unreadable; signed out")
                    self._tokens = None
                self._tokens_loaded = True
            return self._tokens

    def _store_tokens(self, tokens: Tokens | None) -> None:
        with self._lock:
            if tokens is None:
                self._delete(TOKEN_ACCOUNT)
            else:
                self._set(TOKEN_ACCOUNT, json.dumps(asdict(tokens)))
            self._tokens, self._tokens_loaded = tokens, True

    # --- state ----------------------------------------------------------------

    def signed_in(self) -> bool:
        return self._load_tokens() is not None

    def plan_granted(self) -> bool:
        tokens = self._load_tokens()
        return tokens is not None and tokens.plan_granted

    # --- sign in ----------------------------------------------------------------

    def sign_in(
        self,
        open_url: Callable[[str], None],
        *,
        cancel: threading.Event | None = None,
        consent: bool = False,
        timeout: float = SIGN_IN_TIMEOUT_S,
    ) -> Account:
        """The whole browser flow; blocks until the redirect arrives (or cancel /
        timeout). Raises SignInError."""
        endpoints = self.endpoints()
        account = self.account()
        client_id = account.client_id or None
        verifier, challenge = make_pkce()
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        server = CallbackServer(state)
        try:
            open_url(
                authorize_url(
                    endpoints,
                    client_id=client_id,
                    host_id=account.host_id,
                    redirect_uri=server.redirect_uri,
                    state=state,
                    nonce=nonce,
                    challenge=challenge,
                    consent=consent,
                    login_hint=account.email or None,
                )
            )
            query = server.wait(timeout, cancel)
        finally:
            server.close()
        if query.get("error"):
            if query["error"] == "access_denied":
                raise SignInError("declined", "Sign-in was cancelled in the browser")
            raise SignInError("failed", "OpenAI couldn't complete the sign-in")
        issued = query.get("client_id")
        if client_id is None:
            if not issued:
                raise SignInError("failed", "OpenAI didn't finish registering Peek; try again")
            client_id = issued
            with self._lock:
                account.client_id = issued  # saved before the code exchange (docs)
                self._save_account()
        elif issued and issued != client_id:
            raise SignInError("failed", "OpenAI returned an unexpected app registration")
        code = query.get("code")
        if not code:
            raise SignInError("failed", "OpenAI didn't return a sign-in code")
        data = self._post_token(
            endpoints,
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": server.redirect_uri,
                "resource": RESOURCE,
            },
        )
        if "error" in data:
            raise SignInError("failed", "OpenAI didn't accept the sign-in code; try again")
        tokens = Tokens.from_response(data, self._clock())
        claims = _verify_id_token(
            tokens.id_token,
            client_id=client_id,
            nonce=nonce,
            endpoints=endpoints,
            jwks_client=self._jwks(endpoints),
        )
        if account.sub and claims["sub"] != account.sub:
            # Peek's registration belongs to the account it was first set up with
            # (docs: confirm the identity before replacing credentials).
            self._revoke(tokens.refresh_token, client_id)
            raise SignInError(
                "other_account",
                "That's a different ChatGPT account from the one Peek is set up with",
            )
        with self._lock:
            previous = self._load_tokens()
            account.sub = str(claims["sub"])
            account.email = str(claims.get("email") or account.email)
            self._save_account()
            self._store_tokens(tokens)
        if previous is not None and previous.refresh_token != tokens.refresh_token:
            self._revoke(previous.refresh_token, client_id)  # the replaced session ends
        log.info(
            "Signed in to ChatGPT (plan usage %s)",
            "granted" if tokens.plan_granted else "not granted",
        )
        return account

    def _post_token(self, endpoints: Endpoints, form: dict) -> dict:
        try:
            response = self._client().post(
                endpoints.token, data=form, headers={"Accept": "application/json"}
            )
        except Exception:
            raise ProviderError("network", "Can't reach OpenAI; check your connection") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 500:
            raise ProviderError("server", "OpenAI's sign-in is having problems; try again")
        if response.status_code >= 400:
            return {"error": _error_code(data) or str(response.status_code)}
        return data if isinstance(data, dict) else {}

    # --- tokens for requests -----------------------------------------------------

    def access_token(self) -> str:
        """A valid access token, refreshed when it is about to expire. Raises
        ProviderError: signed_out, plan_declined, network, server."""
        with self._lock:
            tokens = self._load_tokens()
            if tokens is None:
                raise ProviderError("signed_out", "Sign in with ChatGPT in Settings")
            if not tokens.plan_granted:
                raise ProviderError(
                    "plan_declined", "ChatGPT plan usage wasn't allowed; allow it in Settings"
                )
            now = self._clock()
            if tokens.expires_at - now <= REFRESH_MARGIN_S and now >= tokens.earliest_refresh_at:
                try:
                    tokens = self._refresh(tokens)
                except SignInError as exc:
                    raise ProviderError("failed", str(exc)) from None
            return tokens.access_token

    def _refresh(self, tokens: Tokens) -> Tokens:
        """Under the lock: one refresh at a time, so a rotated token is never reused."""
        endpoints = self.endpoints()
        account = self.account()
        data = self._post_token(
            endpoints,
            {
                "grant_type": "refresh_token",
                "client_id": account.client_id,
                "refresh_token": tokens.refresh_token,
                "resource": RESOURCE,
            },
        )
        if "error" in data:
            if data["error"] in TERMINAL_REFRESH_ERRORS:
                log.info("ChatGPT session ended (%s); signed out", data["error"])
                self._store_tokens(None)
                raise ProviderError("signed_out", "Your ChatGPT sign-in expired; sign in again")
            # Not a known terminal code: keep the tokens, try again next time.
            log.warning("ChatGPT refresh failed (%s)", data["error"])
            raise ProviderError("failed", "OpenAI couldn't refresh the sign-in; try again")
        fresh = Tokens.from_response(data, self._clock(), previous=tokens)
        # The old refresh token is used up: save the replacement before anything
        # else can fail (a JWKS fetch, a verification error).
        self._store_tokens(fresh)
        if data.get("id_token"):
            try:
                claims = _verify_id_token(
                    fresh.id_token,
                    client_id=account.client_id,
                    nonce=None,
                    endpoints=endpoints,
                    jwks_client=self._jwks(endpoints),
                )
            except SignInError:
                # Usually the JWKS could not be fetched. The tokens came from the
                # token endpoint over TLS; keep them and check again next refresh.
                log.warning("Refreshed ID token not verified; tokens kept")
                return fresh
            if account.sub and claims["sub"] != account.sub:
                self._store_tokens(None)
                raise ProviderError("signed_out", "The ChatGPT account changed; sign in again")
        return fresh

    # --- sign out ------------------------------------------------------------------

    def sign_out(self) -> bool:
        """Clear the tokens, then revoke the refresh token. True if OpenAI confirmed
        the revocation. Raises if the Keychain item could not be deleted (then the
        sign-in is still stored and the caller must not say "signed out"). The
        client ID and host ID are kept for the next sign-in."""
        with self._lock:  # no network under the lock: captures may be waiting on it
            tokens = self._load_tokens()
            client_id = self.account().client_id
            self._store_tokens(None)
        confirmed = tokens is None or self._revoke(tokens.refresh_token, client_id)
        log.info(
            "Signed out of ChatGPT (revocation %s)", "confirmed" if confirmed else "not confirmed"
        )
        return confirmed

    def drop_tokens(self) -> None:
        """OpenAI rejected the tokens: forget them locally (nothing to revoke)."""
        with self._lock:
            if self._load_tokens() is not None:
                self._store_tokens(None)
                log.info("ChatGPT tokens rejected by OpenAI; signed out")

    def forget_account(self) -> None:
        """Sign out and forget the registration (client ID, email, sub), so the
        next sign-in can be a different ChatGPT account. The host ID is kept."""
        self.sign_out()
        with self._lock:
            account = self.account()
            account.client_id = account.email = account.sub = ""
            account.plan_notice_shown = False
            self._save_account()

    def _revoke(self, refresh_token: str, client_id: str) -> bool:
        """Best effort, two tries with a short timeout. True if OpenAI confirmed."""
        for attempt in range(2):
            try:
                response = self._client().post(
                    self.endpoints().revoke,
                    data={
                        "token": refresh_token,
                        "token_type_hint": "refresh_token",
                        "client_id": client_id,
                    },
                    timeout=10.0,
                )
                if response.status_code < 500:
                    return response.status_code == 200
            except Exception:
                log.warning("Revoking a ChatGPT token failed (attempt %d)", attempt + 1)
            time.sleep(0.5 * (attempt + 1))
        return False

    def state(self) -> tuple[bool, bool, str, bool]:
        """(signed in, plan granted, email, plan notice shown), for the UI thread
        to show without touching the lock itself."""
        tokens = self._load_tokens()
        account = self.account()
        return (
            tokens is not None,
            tokens is not None and tokens.plan_granted,
            account.email,
            account.plan_notice_shown,
        )

    def mark_plan_notice_shown(self) -> None:
        with self._lock:
            self.account().plan_notice_shown = True
            self._save_account()


def _error_code(data) -> str:
    """The machine code of an OAuth error body: {"error": "invalid_grant"},
    {"error": {"code": …}}, {"code": …} or {"detail": {"code": …}} (the DevKit
    accepts all of these)."""
    if not isinstance(data, dict):
        return ""
    for key in ("error", "detail"):
        value = data.get(key)
        if isinstance(value, dict):
            code = value.get("code") or value.get("error")
            if code:
                return str(code)
        elif isinstance(value, str) and key == "error":
            return value
    return str(data.get("code") or "")


class ChatGPTCredential:
    """What the pipeline reads like an API key: `get()` returns a fresh access token.
    `signed_in()` is the cheap check the app uses at launch."""

    def __init__(self, session: ChatGPTSession) -> None:
        self.session = session

    def get(self) -> str | None:
        return self.session.access_token()

    def signed_in(self) -> bool:
        return self.session.signed_in() and self.session.plan_granted()
