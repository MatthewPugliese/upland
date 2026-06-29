"""
EOS account → Upland username lookup.
Reads from data/username_cache.json built by scraper/build_username_cache.py.
"""
import json
import os
from pathlib import Path

_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "username_cache.json"
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        if _cache_path().exists():
            try:
                _cache = json.loads(_cache_path().read_text())
            except Exception:
                _cache = {}
        else:
            _cache = {}
    return _cache


def _cache_path() -> Path:
    override = os.environ.get("USERNAME_CACHE")
    return Path(override) if override else _CACHE_PATH


def lookup(eos_account: str) -> str | None:
    """Return Upland username for an EOS account, or None if unknown."""
    return _load().get(eos_account)


def lookup_many(accounts: list[str]) -> dict[str, str]:
    """Return {eos_account: username} for all accounts that are known."""
    cache = _load()
    return {a: cache[a] for a in accounts if a in cache}


def reload() -> int:
    """Force reload of the cache from disk. Returns number of entries."""
    global _cache
    _cache = None
    return len(_load())
