"""Sign-in through Neon Managed Better Auth, proxied by FastAPI.

Mirrors Neon's own server SDK proxy (github.com/neondatabase/neon-js, packages/auth/src/server): the same
forwarded headers, the same cookie rewriting and the same OAuth session-verifier exchange, so the browser
only ever talks to this API and every auth cookie is first-party. Docs: https://neon.com/docs/auth/overview
"""
import hashlib
import json
import re
import time
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import config

COOKIE_PREFIX = "__Secure-neon-auth"
# Neon still accepts a legacy misspelled challenge cookie; recognise both.
CHALLENGE_COOKIES = {f"{COOKIE_PREFIX}.session_challenge", f"{COOKIE_PREFIX}.session_challange"}
VERIFIER_PARAM = "neon_auth_session_verifier"
FORWARD_REQUEST_HEADERS = ("user-agent", "authorization", "referer", "content-type")
# content-encoding is not forwarded: httpx has already decoded the body we pass on.
FORWARD_RESPONSE_HEADERS = ("content-type", "date", "set-auth-jwt", "set-auth-token", "x-neon-ret-request-id")
AUTH_PATH = re.compile(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*")  # no dots: nothing like ../ reaches upstream
ERROR_CODES = {502: "AUTH_UNAVAILABLE", 503: "AUTH_NOT_CONFIGURED"}
SESSION_TTL_S = 60

router = APIRouter(prefix="/api/auth")
_client: httpx.AsyncClient | None = None


def _new_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    # Shared by every user: its cookie jar must refuse all cookies, or one user's Neon session cookie
    # (from a Set-Cookie we relay) would be sent upstream on the next user's request.
    no_cookies = CookieJar(DefaultCookiePolicy(allowed_domains=[]))
    return httpx.AsyncClient(timeout=15, transport=transport, cookies=no_cookies)


def http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = _new_client()
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def neon_cookies(cookie_header: str) -> str:
    """Only Neon Auth cookies travel upstream."""
    pairs = (p.strip() for p in cookie_header.split(";"))
    return "; ".join(p for p in pairs if p.startswith(COOKIE_PREFIX) and "=" in p)


def rewrite_set_cookie(header: str, domain: str | None = None) -> str:
    """Make an upstream cookie first-party: no Partitioned, no upstream Domain, always Secure + HttpOnly, Lax."""
    name_value, *attrs = (part.strip() for part in header.split(";"))
    drop = {"partitioned", "secure", "httponly", "samesite", "domain"}
    kept = [a for a in attrs if a and a.split("=", 1)[0].strip().lower() not in drop]
    return "; ".join([name_value, *kept, "HttpOnly", "Secure", "SameSite=Lax",
                      *([f"Domain={domain}"] if domain else [])])


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def safe_next(target: str | None) -> str:
    """Only send people back into our own frontend (no open redirect)."""
    base = config.FRONTEND_URL
    if target and (target == base or target.startswith(base + "/")):
        return target
    return f"{base}/app"


def _upstream_headers(request: Request) -> dict:
    headers = {name: request.headers[name] for name in FORWARD_REQUEST_HEADERS if name in request.headers}
    # Same order as the SDK: Origin header, then the Referer's origin, then this request's own origin.
    headers["origin"] = (request.headers.get("origin") or _origin(request.headers.get("referer", ""))
                         or _origin(str(request.url)))
    if cookies := neon_cookies(request.headers.get("cookie", "")):
        headers["cookie"] = cookies
    headers["x-neon-auth-middleware"] = "true"
    return headers


async def _call(method: str, path: str, *, query: str, headers: dict, body: bytes | None) -> httpx.Response:
    if not config.NEON_AUTH_URL:
        raise HTTPException(503, "Sign-in isn't configured on this server (set NEON_AUTH_URL).")
    url = f"{config.NEON_AUTH_URL}/{path}" + (f"?{query}" if query else "")
    try:
        return await http().request(method, url, headers=headers, content=body)
    except httpx.HTTPError as e:
        raise HTTPException(502, "Couldn't reach the sign-in service. Try again in a moment.") from e


def _copy_cookies(upstream: httpx.Response, response: Response) -> Response:
    for cookie in upstream.headers.get_list("set-cookie"):
        response.headers.append("set-cookie", rewrite_set_cookie(cookie, config.COOKIE_DOMAIN))
    return response


class SessionCache:
    """get-session results kept SESSION_TTL_S seconds, keyed by a hash of the Neon cookie string."""

    def __init__(self, ttl: float = SESSION_TTL_S):
        self.ttl = ttl
        self.items: dict[str, tuple[float, dict]] = {}

    @staticmethod
    def _key(cookies: str) -> str:
        return hashlib.sha256(cookies.encode()).hexdigest()

    def get(self, cookies: str) -> dict | None:
        hit = self.items.get(self._key(cookies))
        return hit[1] if hit and hit[0] > time.monotonic() else None

    def put(self, cookies: str, user: dict) -> None:
        if len(self.items) > 10_000:  # ponytail: crude bound for one instance; use Redis when scaling out
            self.items.clear()
        self.items[self._key(cookies)] = (time.monotonic() + self.ttl, user)

    def drop(self, cookies: str) -> None:
        self.items.pop(self._key(cookies), None)


sessions = SessionCache()


async def current_user(request: Request) -> dict:
    """FastAPI dependency: the signed-in user, or 401."""
    cookies = neon_cookies(request.headers.get("cookie", ""))
    if not cookies:
        raise HTTPException(401, "Sign in to continue.")
    if (user := sessions.get(cookies)) is not None:
        return user
    upstream = await _call("GET", "get-session", query="", body=None, headers={
        "cookie": cookies, "origin": config.FRONTEND_URL, "x-neon-auth-middleware": "true"})
    try:
        body = upstream.json() if upstream.status_code == 200 else None
    except ValueError:
        body = None
    user = body.get("user") if isinstance(body, dict) else None
    if not user:
        raise HTTPException(401, "Sign in to continue.")
    sessions.put(cookies, user)
    return user


async def provider_token(request: Request, provider: str) -> str | None:
    """The signed-in user's OAuth access token for `provider` (e.g. "github") from Better Auth's
    get-access-token, which refreshes an expired token first. None when they have no such account."""
    upstream = await _call("POST", "get-access-token", query="", body=json.dumps({"providerId": provider}).encode(),
                           headers={"cookie": neon_cookies(request.headers.get("cookie", "")),
                                    "origin": config.FRONTEND_URL, "content-type": "application/json",
                                    "x-neon-auth-middleware": "true"})
    if upstream.status_code == 401:
        raise HTTPException(401, "Sign in to continue.")
    try:
        body = upstream.json() if upstream.status_code == 200 else None
    except ValueError:
        body = None
    return (body.get("accessToken") if isinstance(body, dict) else None) or None


@router.get("/complete")
async def complete(request: Request) -> Response:
    """OAuth return: trade Neon's session verifier + our challenge cookie for session cookies."""
    target = safe_next(request.query_params.get("next"))
    if VERIFIER_PARAM not in request.query_params:
        return RedirectResponse(target, status_code=302)
    failed = RedirectResponse(f"{config.FRONTEND_URL}/signin?error=oauth", status_code=302)
    names = {pair.split("=", 1)[0] for pair in neon_cookies(request.headers.get("cookie", "")).split("; ")}
    if names.isdisjoint(CHALLENGE_COOKIES):
        return failed
    try:  # same query as the SDK sends: the whole callback query, verifier included
        upstream = await _call("GET", "get-session", query=request.url.query, headers=_upstream_headers(request),
                               body=None)
    except HTTPException:
        return failed
    if upstream.status_code != 200 or not upstream.headers.get_list("set-cookie"):
        return failed
    return _copy_cookies(upstream, RedirectResponse(target, status_code=302))


@router.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request) -> Response:
    if not AUTH_PATH.fullmatch(path):
        raise HTTPException(404, "Not found.")
    headers = _upstream_headers(request)
    try:
        upstream = await _call(request.method, path, query=request.url.query, headers=headers,
                               body=await request.body() or None)
    except HTTPException as e:  # Better Auth's error shape, so the client shows the message
        return JSONResponse({"code": ERROR_CODES[e.status_code], "message": e.detail}, status_code=e.status_code)
    if path == "sign-out":
        sessions.drop(headers.get("cookie", ""))
    response = Response(content=upstream.content, status_code=upstream.status_code)
    for name in FORWARD_RESPONSE_HEADERS:
        if name in upstream.headers:
            response.headers[name] = upstream.headers[name]
    return _copy_cookies(upstream, response)
