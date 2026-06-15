"""
UplandScope — Configuration
"""

import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
WEBAPP_DIR = Path(__file__).resolve().parent
NEIGHBORHOOD_MAP_DIR = BASE_DIR / "neighborhood-map"

CACHE_DIR = Path(os.environ.get("CACHE_DIR", str(WEBAPP_DIR / "cache")))
MAPS_DIR = Path(os.environ.get("MAPS_DIR", str(WEBAPP_DIR / "maps")))

CACHE_DIR.mkdir(parents=True, exist_ok=True)
MAPS_DIR.mkdir(parents=True, exist_ok=True)

# ── Load .env before anything imports neighborhood_map ─────────────────────
_ENV_CANDIDATES = [
    Path(os.environ.get("ENV_FILE", "")),
    BASE_DIR / "upland-monitor" / ".env",
    BASE_DIR / ".env",
    WEBAPP_DIR / ".env",
]

for _env in _ENV_CANDIDATES:
    if _env.exists() and _env.is_file():
        with open(_env) as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                k, _, v = _line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        break

# ── App settings ───────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("SECRET_KEY", "uplandscope-dev-key-change-in-prod")
MAP_TTL_HOURS = int(os.environ.get("MAP_TTL_HOURS", "6"))
NEIGHBORHOOD_LIST_TTL = int(os.environ.get("NEIGHBORHOOD_LIST_TTL", "86400"))
MAX_CONCURRENT_GENERATIONS = int(os.environ.get("MAX_CONCURRENT_GENERATIONS", "2"))
