"""Google OAuth 2.0 helpers for YouTube Data API access.

Two flows live here:

1. ``refresh_access_token`` - non-interactive refresh used by the scheduled
   GitHub Actions run (refresh token -> short-lived access token).
2. ``build_auth_url`` / ``exchange_code`` - the one-time interactive flow used
   by ``get_refresh_token.py`` to mint the refresh token in the first place.

Only the ``youtube.readonly`` scope is requested, which is enough to read the
channel, the uploads playlist, video metadata and comments.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

#: Read-only access is all AutoShortSync needs.
DEFAULT_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


class GoogleOAuthError(RuntimeError):
    """Raised when Google rejects a token request."""


def refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    scopes: list[str] | None = None,
) -> tuple[str, int]:
    """Exchange a stored refresh token for a fresh access token.

    Returns ``(access_token, expires_in_seconds)``.
    """
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise GoogleOAuthError(
            f"Token refresh failed (HTTP {exc.code}): {detail.strip()}"
        ) from exc
    except urllib.error.URLError as exc:
        raise GoogleOAuthError(f"Could not reach Google: {exc}") from exc

    if "access_token" not in payload:
        raise GoogleOAuthError(f"Token response missing access_token: {payload}")

    return payload["access_token"], int(payload.get("expires_in", 3600))


def build_auth_url(
    client_id: str,
    redirect_uri: str,
    scopes: list[str] | None = None,
    state: str = "autoshortsync",
) -> str:
    """URL the user must open once to grant access (with consent screen)."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes or DEFAULT_SCOPES),
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",  # force the consent screen so a refresh token is returned
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict:
    """Exchange the one-time authorization code for tokens."""
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise GoogleOAuthError(
            f"Code exchange failed (HTTP {exc.code}): {detail.strip()}"
        ) from exc
