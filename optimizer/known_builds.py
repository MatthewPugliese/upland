"""Builds that are finished but still missing from the structures caches."""

KNOWN_BUILDS = (
    {
        "neighborhood": "dongan hills",
        "address": "114 SEAVIEW AVE",
        "structure": "Barn & Nobles",
    },
)


def _hood(name: str) -> str:
    return (name or "").replace("_", " ").strip().lower()


def missing_builds(neighborhood: str, props: list, structures: dict) -> list[dict]:
    """Known buildings whose property is still an empty list in this cache."""
    hood = _hood(neighborhood)
    address_by_id = {
        str(prop.get("id") or ""): (prop.get("address") or "").upper().strip()
        for prop in props or []
    }
    found = []
    for gap in KNOWN_BUILDS:
        if gap["neighborhood"] != hood:
            continue
        for prop_id, address in address_by_id.items():
            if address != gap["address"]:
                continue
            buildings = structures.get(prop_id) or []
            names = {
                (building.get("buildingName") or "")
                for building in buildings
                if isinstance(building, dict)
            }
            if gap["structure"] in names:
                continue
            found.append({
                "prop_id": prop_id,
                "address": gap["address"],
                "structure": gap["structure"],
            })
    return found


def apply_missing_builds(neighborhood: str, props: list, structures: dict) -> dict:
    """Count known buildings in this in-memory cache. Does not write the file."""
    if not isinstance(structures, dict):
        return structures
    for gap in missing_builds(neighborhood, props, structures):
        structures.setdefault(gap["prop_id"], []).append({
            "buildingName": gap["structure"],
            "constructionStatus": "completed",
        })
    return structures
