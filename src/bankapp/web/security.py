"""Same-origin enforcement for the local dashboard's write routes.

The app binds 127.0.0.1 only, but "localhost-only" does NOT stop a malicious web
page the user has open in the same browser from POSTing to us: a bare cross-origin
<form> is a "simple request" that fires with no CORS preflight, and Starlette parses
a JSON body without requiring `Content-Type: application/json`, so a `text/plain`
form forgery reaches the JSON routes too. Without a guard, any page the user visits
while the dashboard is running could archive goals, create goals, or add rules.

Defense: for state-changing methods, require the request to be same-origin. We key
off headers a browser sets and page JavaScript cannot forge cross-origin:

- ``Sec-Fetch-Site`` (sent by all current browsers): must be ``same-origin`` or
  ``none`` (a direct navigation / address-bar load). ``cross-site`` / ``same-site``
  from another origin is rejected.
- ``Origin`` (fallback for the rare browser without Fetch Metadata): its host must be
  loopback.
- ``Host`` (only when the request looks browser-originated): must be loopback, which
  closes DNS-rebinding — a rebound page is same-origin to the browser but still
  carries the attacker's hostname in ``Host``.

Non-browser clients — the ``categorize`` skill, ``curl``, the test client — send none
of these headers and are allowed through, preserving the CLI/skill write path that the
categorization design depends on.
"""

from __future__ import annotations

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# The dashboard only ever binds loopback (see web/app.py serve()), so the sole
# legitimate host/origin is one of these names (any port).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _hostname(value: str) -> str:
    """Bare hostname from a Host header or Origin URL, port stripped, lowercased."""
    value = (value or "").strip().lower()
    if "://" not in value:
        value = "//" + value
    return urlparse(value).hostname or ""


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Reject cross-origin state-changing requests (CSRF); pass reads and non-browser clients."""

    def __init__(self, app, allowed_hosts: frozenset[str] = _LOOPBACK_HOSTS):
        super().__init__(app)
        self._allowed = allowed_hosts

    async def dispatch(self, request: Request, call_next):
        if request.method not in _UNSAFE_METHODS:
            return await call_next(request)

        origin = request.headers.get("origin")
        sec_fetch_site = request.headers.get("sec-fetch-site")
        is_browser = sec_fetch_site is not None or origin is not None

        if is_browser:
            # DNS-rebinding: a rebound page reads as same-origin but Host is the attacker's.
            if _hostname(request.headers.get("host", "")) not in self._allowed:
                return _forbidden("host not allowed")
            if sec_fetch_site is not None:
                if sec_fetch_site not in ("same-origin", "none"):
                    return _forbidden("cross-origin request blocked")
            elif origin is not None and _hostname(origin) not in self._allowed:
                return _forbidden("cross-origin request blocked")
        # No browser-context headers => a non-browser client (CLI/skill/tests): allow.
        return await call_next(request)


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=403)
