"""Claude subscription plan-usage probe.

Fetches the same data Claude Code shows in its ``/usage`` panel — the 5-hour
session limit, the weekly all-models limit and the weekly Sonnet-only limit —
from the OAuth endpoint ``GET https://api.anthropic.com/api/oauth/usage``.

The OAuth access token is read from the credentials file shared with the Claude
CLI (``~/.claude/.credentials.json`` → ``claudeAiOauth``). When the token is
expired (or rejected with 401) it is refreshed via
``POST https://platform.claude.com/v1/oauth/token`` and written back atomically
with a one-time ``.bak`` backup — mirroring Claude Code's own refresh flow.

All secrets stay in ``~/.claude`` (never the repo). Endpoint/headers/client-id
were reverse-engineered from the Claude Code CLI bundle (cli.js 2.1.x).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import structlog

logger = structlog.get_logger()

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Claude Code's public OAuth client id (from cli.js M4().CLIENT_ID).
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA = "oauth-2025-04-20"
SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers"
USER_AGENT = "claude-cli/2.1.31 (external, cli)"

# Refresh slightly before the hard expiry to avoid racing a 401.
_EXPIRY_SKEW_MS = 60_000


class UsageError(Exception):
    """Plan usage could not be fetched."""


@dataclass
class LimitWindow:
    """A single rate-limit window (percent used + when it resets)."""

    percent: float  # 0..100
    resets_at: Optional[str] = None  # ISO-8601 string, UTC


@dataclass
class PlanUsage:
    """Snapshot of subscription plan usage."""

    five_hour: Optional[LimitWindow] = None
    seven_day: Optional[LimitWindow] = None
    seven_day_sonnet: Optional[LimitWindow] = None
    seven_day_opus: Optional[LimitWindow] = None
    subscription_type: Optional[str] = None


def _creds_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / ".credentials.json"


def _load_creds() -> Dict[str, Any]:
    path = _creds_path()
    if not path.is_file():
        raise UsageError(f"credentials file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read credentials: {exc}") from exc


def _save_creds_atomic(data: Dict[str, Any]) -> None:
    """Write credentials back atomically, keeping a one-time .bak backup."""
    path = _creds_path()
    backup = path.with_suffix(".json.bak")
    try:
        if path.is_file() and not backup.exists():
            shutil.copy2(path, backup)
    except OSError:
        pass  # backup is best-effort; never block a refresh on it
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)  # atomic on the same filesystem
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


async def _refresh_token(creds: Dict[str, Any]) -> str:
    """Exchange the refresh token for a fresh access token; persist it."""
    oauth = creds.get("claudeAiOauth") or {}
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise UsageError("no refresh token in credentials")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
                "scope": SCOPES,
            },
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
    if resp.status_code != 200:
        raise UsageError(f"token refresh failed: HTTP {resp.status_code}")
    tok = resp.json()
    access = tok.get("access_token")
    if not access:
        raise UsageError("token refresh returned no access_token")

    oauth["accessToken"] = access
    if tok.get("refresh_token"):
        oauth["refreshToken"] = tok["refresh_token"]
    if tok.get("expires_in"):
        oauth["expiresAt"] = int(time.time() * 1000) + int(tok["expires_in"]) * 1000
    creds["claudeAiOauth"] = oauth
    _save_creds_atomic(creds)
    logger.info("Refreshed Claude OAuth token for plan-usage probe")
    return access


async def _access_token(creds: Dict[str, Any]) -> str:
    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise UsageError("no access token in credentials")
    expires_at = int(oauth.get("expiresAt") or 0)
    now_ms = int(time.time() * 1000)
    if expires_at and expires_at - now_ms < _EXPIRY_SKEW_MS:
        return await _refresh_token(creds)
    return token


def _parse_window(obj: Any) -> Optional[LimitWindow]:
    if not isinstance(obj, dict):
        return None
    util = obj.get("utilization")
    if util is None:
        return None
    return LimitWindow(percent=float(util), resets_at=obj.get("resets_at"))


async def fetch_plan_usage() -> PlanUsage:
    """Return the current subscription plan usage snapshot.

    Raises :class:`UsageError` if credentials are missing/unusable or the
    endpoint cannot be reached.
    """
    creds = _load_creds()
    token = await _access_token(creds)

    async def _call(bearer: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=10) as client:
            return await client.get(
                USAGE_URL,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                    "Authorization": f"Bearer {bearer}",
                    "anthropic-beta": OAUTH_BETA,
                },
            )

    resp = await _call(token)
    if resp.status_code == 401:
        # Stale token slipped past the expiry check — refresh once and retry.
        token = await _refresh_token(creds)
        resp = await _call(token)
    if resp.status_code != 200:
        raise UsageError(f"usage endpoint HTTP {resp.status_code}")

    data = resp.json()
    return PlanUsage(
        five_hour=_parse_window(data.get("five_hour")),
        seven_day=_parse_window(data.get("seven_day")),
        seven_day_sonnet=_parse_window(data.get("seven_day_sonnet")),
        seven_day_opus=_parse_window(data.get("seven_day_opus")),
        subscription_type=(creds.get("claudeAiOauth") or {}).get("subscriptionType"),
    )
