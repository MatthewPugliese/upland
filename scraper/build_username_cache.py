#!/usr/bin/env python3
"""
Build EOS-account → Upland-username lookup cache by scanning a4 notarization
actions on the blockchain.

Each a4 action memo contains:
    "This transaction notarizes that Upland user {username} with corresponding
     EOS account {eos_account} owns ..."

We scan the full history and store the most-recent username for each EOS account.

Output: data/username_cache.json  { "eos_account": "username", ... }

Usage:
    python3 build_username_cache.py            # scan AppChain + EOS
    python3 build_username_cache.py --chain app   # AppChain only (faster)
    python3 build_username_cache.py --resume      # pick up where we left off
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

APPCHAIN_URL   = "https://chain-history.upland.me"
EOS_URL        = "https://eos.hyperion.eosrio.io"
APPCHAIN_START = "2025-04-28T00:00:00.000"
EOS_START      = "2023-03-18T00:00:00.000"
APPCHAIN_END   = "2025-04-28T00:00:00.000"   # EOS chain stops at AppChain start

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; upland-scope/1.0)",
    "Accept": "application/json",
}

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR   = SCRIPT_DIR.parent / "data"
CACHE_PATH = DATA_DIR / "username_cache.json"
STATE_PATH = DATA_DIR / "username_cache_state.json"

BATCH = 1000
RATE  = 5     # req/s
SAVE_EVERY = 20  # save every N pages

MEMO_RE = re.compile(
    r"Upland user\s+(\S+)\s+with corresponding EOS account\s+(\S+)",
    re.IGNORECASE,
)


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _save_json(path: Path, data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def fetch_a4_page(base_url: str, after: str, before: str = None) -> list:
    params = [
        "account=playuplandme",
        "filter=playuplandme:a4",
        f"after={after}",
        f"limit={BATCH}",
        "sort=asc",
    ]
    if before:
        params.append(f"before={before}")
    url = f"{base_url}/v2/history/get_actions?" + "&".join(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read()).get("actions", [])


def scan_chain(base_url: str, chain_name: str, chain_start: str,
               chain_end: str | None, cache: dict, cursor: str) -> tuple[dict, str]:
    """
    Scan a4 actions on this chain from cursor onward.
    Returns (updated_cache, final_cursor).
    """
    total_new = 0
    pages = 0
    last_ts = cursor

    while True:
        try:
            actions = fetch_a4_page(base_url, after=last_ts, before=chain_end)
        except Exception as e:
            print(f"  [!] {chain_name} fetch error at {last_ts}: {e}", flush=True)
            time.sleep(5)
            continue

        if not actions:
            print(f"  [{chain_name}] no more actions — done", flush=True)
            break

        for action in actions:
            ts   = action.get("timestamp", "")
            act  = action.get("act", {})
            data = act.get("data", {})
            memo = data.get("memo", "")
            m    = MEMO_RE.search(memo)
            if m:
                username = m.group(1)
                eos_acct = m.group(2)
                # Always overwrite — later entry is more recent username
                cache[eos_acct] = username
                total_new += 1
            if ts > last_ts:
                last_ts = ts

        pages += 1
        if pages % SAVE_EVERY == 0:
            _save_json(CACHE_PATH, cache)
            print(
                f"  [{chain_name}] page {pages} — cursor {last_ts[:19]} — "
                f"{len(cache):,} accounts, {total_new} new this run",
                flush=True,
            )

        # If we got fewer than BATCH, we've reached the end
        if len(actions) < BATCH:
            print(f"  [{chain_name}] reached end of history ({len(actions)} on last page)", flush=True)
            break

        time.sleep(1 / RATE)

    print(f"  [{chain_name}] finished — {total_new} new mappings, total {len(cache):,}", flush=True)
    return cache, last_ts


def main():
    parser = argparse.ArgumentParser(description="Build EOS→username cache")
    parser.add_argument("--chain", choices=["app", "eos", "both"], default="both")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from saved state cursor")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache = _load_json(CACHE_PATH)
    state = _load_json(STATE_PATH) if args.resume else {}
    print(f"[*] Starting with {len(cache)} cached mappings", flush=True)

    chains = []
    if args.chain in ("app", "both"):
        chains.append(("AppChain", APPCHAIN_URL, APPCHAIN_START, None))
    if args.chain in ("eos", "both"):
        chains.append(("EOS", EOS_URL, EOS_START, APPCHAIN_END))

    for name, url, chain_start, chain_end in chains:
        cursor = state.get(f"{name}_cursor", chain_start) if args.resume else chain_start
        print(f"\n[*] Scanning {name} from {cursor[:19]} …", flush=True)
        cache, final_cursor = scan_chain(url, name, chain_start, chain_end, cache, cursor)
        state[f"{name}_cursor"] = final_cursor
        _save_json(CACHE_PATH, cache)
        _save_json(STATE_PATH, state)
        print(f"  [{name}] saved → {CACHE_PATH}", flush=True)

    print(f"\n[+] Done — {len(cache):,} EOS→username mappings in {CACHE_PATH}", flush=True)


if __name__ == "__main__":
    main()
