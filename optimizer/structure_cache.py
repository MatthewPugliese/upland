"""One structures list for the score and the recommendations.

The webapp cache and the optimizer cache can disagree. The newer file wins
for every property it mentions. A property only the older file knows about
keeps that older record. Names that differ from the structure database by
case or surrounding space use the database name. Nothing here writes a cache.
"""

from __future__ import annotations

import json
from pathlib import Path

from structure_fitter import STRUCTURES

_ROOT = Path(__file__).resolve().parent.parent
_OPTIMIZER_CACHE = _ROOT / "optimizer" / "cache"
_WEB_CACHE = _ROOT / "webapp" / "cache" / "neighborhoods"
_CANONICAL = {name.casefold(): name for name in STRUCTURES}


def safe_neighborhood(name: str) -> str:
    return "".join(
        c if c.isalnum() or c in " -_" else "_" for c in (name or "")
    ).strip().replace(" ", "_")


def canonical_name(name: str) -> str:
    cleaned = (name or "").strip()
    return _CANONICAL.get(cleaned.casefold(), cleaned)


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _copy_building(building: dict) -> dict:
    copied = dict(building)
    copied["buildingName"] = canonical_name(building.get("buildingName") or "")
    return copied


def prefer_structures(newer: dict, older: dict) -> dict:
    """Copy `newer`, and fill property ids that `newer` does not mention."""
    merged: dict = {}
    newer = newer or {}
    older = older or {}
    for prop_id, buildings in newer.items():
        if not isinstance(buildings, list):
            merged[prop_id] = buildings
            continue
        merged[prop_id] = [
            _copy_building(building) for building in buildings if isinstance(building, dict)
        ]
    for prop_id, buildings in older.items():
        if prop_id in newer or not isinstance(buildings, list):
            continue
        merged[prop_id] = [
            _copy_building(building) for building in buildings if isinstance(building, dict)
        ]
    return merged


def load_preferred_structures(neighborhood: str, paths: list[Path] | None = None) -> dict | None:
    """Return the merged cache, or None when no structures file exists."""
    if paths is None:
        safe = safe_neighborhood(neighborhood)
        paths = [
            _OPTIMIZER_CACHE / f"{safe}_structures_cache.json",
            _WEB_CACHE / f"{safe}_structures_cache.json",
        ]
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    existing.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    newer = _read(existing[0])
    older = _read(existing[1]) if len(existing) > 1 else {}
    return prefer_structures(newer, older)
