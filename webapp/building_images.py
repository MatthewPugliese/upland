"""
UplandScope — Building Image Cache

Maps building names to their CDN image URLs.
Seed data comes from DH structures; new types are fetched from the
public Upland API on first encounter and persisted to the cache file.
"""

import json
import threading
from pathlib import Path

CDN_BASE = "https://static.upland.me/3d-models/"
CACHE_PATH = Path(__file__).resolve().parent / "cache" / "building_images.json"

_SEED = {
    "Apartment Building": "apartment_new/apartment_baked.png",
    "Arcade": "service_structures/arcade/Arcade_ipfs.png",
    "Bakery": "service_structures/bakery/bakery_ipfs.png",
    "Bodega": "service_structures/grocery_store/grocery_store_ipfs.png",
    "Bus Stop": "service_structures/bus_stop/bus_station_ipfs.png",
    "Contemporary House": "ugc_winners/contemporary_house/contemporary_sz.png",
    "Day Care Center": "service_structures/day_care/ipfs_daycare.png",
    "Dry Cleaner": "service_structures/dry_cleaner/dry_cleaner_ipfs.png",
    "East Coast Modular Apartments: Pharmacy": "service_structures/mainstreet_modular/queens_pharmacy/hybrid_store_corner_queens.png",
    "Family Home": "wonderland_structure/family_home/ranch_house_wonderland_ipfs.png",
    "Fast Food Joint": "service_structures/fast_food_joint/drive_ipfs.png",
    "Fire Station": "service_structures/fire_station/fire_station_ipfs.png",
    "Glass Tower": "frost_season/glass_tower/glass_tower.png",
    "Kiosk - Hot Dog": "service_structures/kiosks/kiosk_hot_dog/hotdog_ipfs.png",
    "Large Showroom I": "showroom/large_showroom/large_showroom_I_sz.png",
    "Luxury Modern House": "luxury_modern_house/manor_thumbnail_sz.png",
    "Luxury Ranch House": "luxury_ranch_house/ranch_manor_thumbnail_sz.png",
    "Medium Showroom I": "showroom/medium_showroom/medium_showroom_I_sz.png",
    "Medium Showroom II": "showroom/medium_showroom_II/MediumShowroomII_IPFS.png",
    "Micro House": "micro_house/micro_house_IPFS_sz.png",
    "Ranch House": "ranch_house_new/ranch_baked.png",
    "Small Factory I": "decor_factory/square_factory_xs_sz.png",
    "Small Town House": "small_town_house_new/small_town_baked.png",
    "Speedway Structure - Medium": "speedway/speedway_structure_medium/speedway_medium.png",
    "Town House": "town_house_new/town_baked.png",
}

_cache: dict = {}
_lock = threading.Lock()
_loaded = False


def _load():
    global _cache, _loaded
    if _loaded:
        return
    _cache = dict(_SEED)
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH) as f:
                _cache.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    _loaded = True


def _save():
    try:
        with open(CACHE_PATH, "w") as f:
            json.dump(_cache, f, indent=2, sort_keys=True)
    except OSError:
        pass


def get_image_url(building_name: str, prop_id: str = None) -> str | None:
    """
    Return the full CDN URL for a building image.
    If not cached and prop_id is given, fetches from the public API.
    Returns None if unknown.
    """
    with _lock:
        _load()
        path = _cache.get(building_name)

    if path:
        return CDN_BASE + path

    if not prop_id:
        return None

    # Try to fetch from public API
    try:
        import requests
        r = requests.get(
            f"https://api.upland.me/properties/{prop_id}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if r.status_code == 200:
            for b in r.json().get("buildings", []):
                if b.get("buildingName") == building_name and b.get("buildingImage"):
                    img_path = b["buildingImage"]
                    with _lock:
                        _cache[building_name] = img_path
                        _save()
                    return CDN_BASE + img_path
    except Exception:
        pass

    return None


def get_all_known() -> dict[str, str]:
    """Return {building_name: full_cdn_url} for all known buildings."""
    with _lock:
        _load()
        return {name: CDN_BASE + path for name, path in _cache.items()}
