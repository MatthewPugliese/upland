"""Monthly UPX from the last on-chain earnings collection, not from mint price."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

CHAIN_URL = "https://chain-history.upland.me"
# One month is 1/12 of a year, same split the mint-price estimate uses.
MONTH_DAYS = 365 / 12
_CACHE_TTL = 6 * 3600
_HEADERS = {"User-Agent": "UplandScope/1.0"}
_RESULT_KEYS = (
    "payout", "monthly", "hourly", "as_of", "elapsed_days",
    "previous_payout", "previous_as_of",
)


def run_rate(payout_upx: float, elapsed_days: float) -> dict | None:
    """Stretch one collection payout across a month."""
    if payout_upx <= 0 or elapsed_days < 0.25:
        return None
    hourly = payout_upx / (elapsed_days * 24)
    monthly = payout_upx * MONTH_DAYS / elapsed_days
    return {
        "payout": round(payout_upx, 2),
        "elapsed_days": elapsed_days,
        "monthly": round(monthly),
        "hourly": round(hourly, 2),
    }


def _epoch(timestamp: str) -> float:
    text = timestamp.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        moment = datetime.strptime(timestamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _as_of(timestamp: str) -> str:
    moment = datetime.fromtimestamp(_epoch(timestamp), timezone.utc)
    return f"{moment.strftime('%b')} {moment.day}, {moment.year}"


def group_rounds(actions: list, max_rounds: int = 2) -> list:
    """Split newest-first n31 actions into collection rounds (a few seconds each)."""
    rounds = []
    current = []
    last = None
    for action in actions:
        moment = _epoch(action.get("@timestamp") or "")
        if last is not None and (last - moment) > 300:
            if current:
                rounds.append(current)
            current = []
            if len(rounds) >= max_rounds:
                break
        current.append(action)
        last = moment
    if current and len(rounds) < max_rounds:
        rounds.append(current)
    return rounds


def _default_actions(eos_account: str) -> list:
    response = requests.get(
        f"{CHAIN_URL}/v2/history/get_actions",
        params={
            "account": eos_account,
            "filter": "playuplandme:n31",
            "limit": 100,
            "sort": "desc",
        },
        headers=_HEADERS,
        timeout=40,
    )
    response.raise_for_status()
    return response.json().get("actions") or []


def _round_trx_ids(actions: list) -> list:
    return list(dict.fromkeys(a.get("trx_id") for a in actions if a.get("trx_id")))


def _payout_for_round(actions: list, eos_account: str, fetch_payout) -> float:
    trx_ids = _round_trx_ids(actions)
    if fetch_payout:
        return float(fetch_payout(trx_ids, eos_account))
    payout = 0.0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for amount in pool.map(lambda trx: _transfer_upx(trx, eos_account), trx_ids):
            payout += amount
    return payout


def _transfer_upx(trx_id: str, eos_account: str) -> float:
    response = requests.get(
        f"{CHAIN_URL}/v2/history/get_transaction",
        params={"id": trx_id},
        headers=_HEADERS,
        timeout=40,
    )
    response.raise_for_status()
    total = 0.0
    for action in response.json().get("actions") or []:
        act = action.get("act") or {}
        if act.get("name") != "transfer":
            continue
        data = act.get("data") or {}
        if data.get("from") != "communityupx" or data.get("to") != eos_account:
            continue
        quantity = str(data.get("quantity") or "")
        if not quantity.endswith("UPX"):
            continue
        total += float(quantity.split()[0])
    return total


def latest_collection_income(
    eos_account: str,
    cache_path: Path | None = None,
    fetch_actions=None,
    fetch_payout=None,
) -> dict | None:
    """Last earnings payout, as a monthly and hourly run-rate.

    Returns None when the chain does not have two collections to measure.
    """
    account = (eos_account or "").strip()
    if not account:
        return None

    now = time.time()
    if cache_path and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            fresh = now - cached.get("cached_at", 0) < _CACHE_TTL and cached.get("monthly")
            # Older caches omit the previous collection. Treat those as stale.
            if fresh and "previous_payout" in cached:
                return {k: cached[k] for k in _RESULT_KEYS}
        except (json.JSONDecodeError, OSError, TypeError):
            pass

    try:
        actions = (fetch_actions or _default_actions)(account)
        rounds = group_rounds(actions)
        if len(rounds) < 2:
            return None
        payout = _payout_for_round(rounds[0], account, fetch_payout)
        latest = rounds[0][0]["@timestamp"]
        previous = rounds[1][0]["@timestamp"]
        elapsed_days = (_epoch(latest) - _epoch(previous)) / 86400
        rated = run_rate(payout, elapsed_days)
        if not rated:
            return None
        previous_payout = None
        previous_as_of = ""
        try:
            previous_payout = round(_payout_for_round(rounds[1], account, fetch_payout), 2)
            previous_as_of = _as_of(previous)
        except (requests.RequestException, ValueError, KeyError, TypeError, ZeroDivisionError):
            previous_payout = None
            previous_as_of = ""
        result = {
            **rated,
            "as_of": _as_of(latest),
            "previous_payout": previous_payout,
            "previous_as_of": previous_as_of,
        }
    except (requests.RequestException, ValueError, KeyError, TypeError, ZeroDivisionError):
        return None

    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({**result, "cached_at": now}))
        except OSError:
            pass
    return result
