"""
UplandScope — Server-side session data store

Flask's default session mechanism signs and stores ALL session data in a
client-side cookie. Collection Tracker analysis data (every owned property
ID + full near-complete-collection details for the whole portfolio) can grow
well past both the ~4KB browser cookie limit and gunicorn's request-header
size limit for large portfolios — confirmed on the Pi 2026-08-16: a
727-property account produced a 22.9KB session cookie, and gunicorn
rejected every *subsequent* request in that session outright with HTTP 431
(Request Header Fields Too Large), never even reaching Flask's routing.

This stores the actual data server-side in small JSON files keyed by a
random token; only the token goes in the session cookie. TTL-based cleanup
(swept opportunistically on every save) prevents unbounded accumulation —
fine for a single-user personal tool, not designed for multi-tenant scale.
"""

import json
import re
import secrets
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STORE_DIR = SCRIPT_DIR / "cache" / "sessions"
TTL_SECONDS = 3600  # 1 hour — comfortably covers a single browsing session
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")  # secrets.token_urlsafe's charset


def _cleanup_stale() -> None:
    if not STORE_DIR.exists():
        return
    now = time.time()
    for f in STORE_DIR.glob("*.json"):
        try:
            if now - f.stat().st_mtime > TTL_SECONDS:
                f.unlink()
        except OSError:
            pass


def save(token: str | None, data: dict) -> str:
    """
    Persist data server-side. Reuses `token` (overwriting) if given and
    still valid, otherwise mints a fresh one. Returns the token to store in
    the session cookie — callers should always update session[...] with it,
    since a stale/unknown token mints a new file rather than erroring.
    """
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    if not token or not _TOKEN_RE.match(token):
        token = secrets.token_urlsafe(16)
    (STORE_DIR / f"{token}.json").write_text(json.dumps(data))
    _cleanup_stale()
    return token


def load(token: str | None) -> dict:
    """Load data by token. Returns {} if missing, expired, or no token given."""
    if not token or not _TOKEN_RE.match(token):
        return {}
    path = STORE_DIR / f"{token}.json"
    if not path.exists():
        return {}
    if time.time() - path.stat().st_mtime > TTL_SECONDS:
        try:
            path.unlink()
        except OSError:
            pass
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}
