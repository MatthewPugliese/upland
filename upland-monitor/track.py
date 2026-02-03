#!/usr/bin/env python3
"""
Upland Appchain Tracker
Polls the Hyperion History API and prints decoded actions to stdout
"""

import requests
import time
import json
from datetime import datetime, timezone

BASE_URL = "https://chain-history.upland.me"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}

# Known action decoders
ACTION_NAMES = {
    "n41": "PROPERTY_VISIT",
    "n43": "GAME_ACTION_N43",
    "payforcpu": "CPU_SPONSOR",
    "transfer": "TRANSFER",
    "onblock": "SYSTEM_BLOCK",
}

# Known parameter names
PARAM_NAMES = {
    "p51": "player",
    "p12": "amount",
    "p54": "fee",
    "a45": "property_id",
}

# Noise actions to skip
SKIP_ACTIONS = {"onblock", "payforcpu"}

def decode_params(data):
    """Decode obfuscated parameter names"""
    decoded = {}
    for k, v in data.items():
        key = PARAM_NAMES.get(k, k)
        decoded[key] = v
    return decoded

def decode_action(action):
    """Decode an action into human-readable format"""
    act = action.get("act", {})
    contract = act.get("account", "")
    name = act.get("name", "")
    data = act.get("data", {})
    
    # Get human-readable action name
    action_type = ACTION_NAMES.get(name, name.upper())
    
    # Decode parameters
    decoded_data = decode_params(data)
    
    # Build result
    result = {
        "time": action.get("@timestamp", "")[:19].replace("T", " "),
        "block": action.get("block_num"),
        "trx_id": action.get("trx_id", ""),
        "type": action_type,
        "contract": contract,
        "raw_action": name,
    }
    
    # Format based on action type
    if name == "transfer":
        result["summary"] = f"{data.get('from')} → {data.get('to')}: {data.get('quantity', data.get('amount', '?'))}"
        result["from"] = data.get("from")
        result["to"] = data.get("to")
        result["quantity"] = data.get("quantity", f"{data.get('amount', '?')} {data.get('symbol', '')}")
        result["memo"] = data.get("memo", "")
    
    elif name == "n41":  # Property visit
        result["summary"] = f"Visit property {decoded_data.get('property_id')} for {decoded_data.get('fee')}"
        result["player"] = decoded_data.get("player")
        result["property_id"] = decoded_data.get("property_id")
        result["fee"] = decoded_data.get("fee")
    
    elif name == "n43":
        result["summary"] = f"Game action by {decoded_data.get('player')} - {decoded_data.get('amount')}"
        result["data"] = decoded_data
    
    else:
        result["data"] = decoded_data
    
    return result

def format_decoded(decoded):
    """Pretty print a decoded action"""
    lines = [
        f"┌─ {decoded['type']} ─────────────────────────",
        f"│ Time:    {decoded['time']}",
        f"│ Block:   {decoded['block']}",
    ]
    
    if "summary" in decoded:
        lines.append(f"│ Summary: {decoded['summary']}")
    
    # Add type-specific fields
    if decoded.get("from"):
        lines.append(f"│ From:    {decoded['from']}")
    if decoded.get("to"):
        lines.append(f"│ To:      {decoded['to']}")
    if decoded.get("quantity"):
        lines.append(f"│ Amount:  {decoded['quantity']}")
    if decoded.get("property_id"):
        lines.append(f"│ Property: {decoded['property_id']}")
    if decoded.get("fee"):
        lines.append(f"│ Fee:     {decoded['fee']}")
    if decoded.get("memo"):
        lines.append(f"│ Memo:    {decoded['memo']}")
    if decoded.get("data") and decoded["type"] not in ["TRANSFER", "PROPERTY_VISIT"]:
        lines.append(f"│ Data:    {json.dumps(decoded['data'], default=str)}")
    
    lines.append(f"└─ trx: {decoded['trx_id'][:32]}...")
    
    return "\n".join(lines)

def get_actions(account=None, limit=25, after=None, action_filter=None):
    """Fetch actions from the Hyperion API"""
    params = {"limit": limit, "sort": "desc"}
    if account:
        params["account"] = account
    if after:
        params["after"] = after
    if action_filter:
        params["filter"] = action_filter
    
    r = requests.get(f"{BASE_URL}/v2/history/get_actions", params=params, headers=HEADERS)
    r.raise_for_status()
    return r.json()

def get_transaction(trx_id):
    """Fetch full transaction by ID"""
    r = requests.get(f"{BASE_URL}/v2/history/get_transaction", params={"id": trx_id}, headers=HEADERS)
    r.raise_for_status()
    return r.json()

def get_chain_health():
    """Check API health"""
    r = requests.get(f"{BASE_URL}/v2/health", headers=HEADERS)
    r.raise_for_status()
    return r.json()

def get_transaction(trx_id):
    """Fetch full transaction by ID"""
    r = requests.get(f"{BASE_URL}/v2/history/get_transaction", params={"id": trx_id}, headers=HEADERS)
    r.raise_for_status()
    return r.json()

def poll_actions(account=None, action_filter=None, interval=5, show_all=False, expand_tx=True):
    """Continuously poll for new actions"""
    print(f"╔═══════════════════════════════════════════════════════")
    print(f"║ Upland Appchain Tracker")
    print(f"║ Account: {account or 'ALL'}")
    print(f"║ Filter:  {action_filter or 'ALL (excluding noise)'}")
    print(f"║ Expand:  {'Yes (fetching full transactions)' if expand_tx else 'No'}")
    print(f"║ Interval: {interval}s")
    print(f"╚═══════════════════════════════════════════════════════")
    
    # Check health
    try:
        health = get_chain_health()
        node = next((h for h in health.get("health", []) if h.get("service") == "NodeosRPC"), {})
        head_block = node.get("service_data", {}).get("head_block_num", "?")
        print(f"[+] Connected - Head block: {head_block}")
    except Exception as e:
        print(f"[!] Health check failed: {e}")
    print()
    
    seen_txs = set()
    seen_full_txs = set()
    last_timestamp = datetime.now(timezone.utc).isoformat()
    
    while True:
        try:
            data = get_actions(
                account=account,
                limit=50,
                after=last_timestamp,
                action_filter=action_filter
            )
            
            actions = data.get("actions", [])
            
            # If expand_tx is enabled, fetch full transactions to get all related actions
            if expand_tx and account:
                expanded_actions = []
                for action in actions:
                    trx_id = action.get("trx_id")
                    if trx_id and trx_id not in seen_full_txs:
                        seen_full_txs.add(trx_id)
                        try:
                            tx_data = get_transaction(trx_id)
                            if tx_data.get("executed"):
                                expanded_actions.extend(tx_data.get("actions", []))
                        except:
                            expanded_actions.append(action)
                    elif trx_id in seen_full_txs:
                        continue
                    else:
                        expanded_actions.append(action)
                actions = expanded_actions
                
                # Prevent memory bloat on seen_full_txs
                if len(seen_full_txs) > 5000:
                    seen_full_txs = set(list(seen_full_txs)[-2500:])
            
            for action in reversed(actions):
                trx_id = action.get("trx_id")
                global_seq = action.get("global_sequence")
                uid = f"{trx_id}:{global_seq}"
                act_name = action.get("act", {}).get("name", "")
                
                # Skip noise unless show_all
                if not show_all and act_name in SKIP_ACTIONS:
                    continue
                
                if uid not in seen_txs:
                    seen_txs.add(uid)
                    decoded = decode_action(action)
                    print(format_decoded(decoded))
                    print()
                    
                    ts = action.get("@timestamp")
                    if ts and ts > last_timestamp:
                        last_timestamp = ts
            
            # Prevent memory bloat
            if len(seen_txs) > 10000:
                seen_txs = set(list(seen_txs)[-5000:])
                
        except requests.exceptions.RequestException as e:
            print(f"[!] Request error: {e}")
        except Exception as e:
            print(f"[!] Error: {e}")
        
        time.sleep(interval)

def dump_recent(account=None, limit=100, show_all=False, raw=False):
    """One-shot dump of recent actions"""
    print(f"[*] Fetching last {limit} actions for {account or 'ALL'}...\n")
    data = get_actions(account=account, limit=limit)
    
    for action in data.get("actions", []):
        act_name = action.get("act", {}).get("name", "")
        
        if not show_all and act_name in SKIP_ACTIONS:
            continue
        
        if raw:
            print(json.dumps(action, indent=2, default=str))
        else:
            decoded = decode_action(action)
            print(format_decoded(decoded))
        print()
    
    print(f"[+] Total in history: {data.get('total', {}).get('value', '?')} actions")

def inspect_transaction(trx_id):
    """Fetch and decode a full transaction"""
    print(f"[*] Fetching transaction {trx_id[:16]}...\n")
    data = get_transaction(trx_id)
    
    if not data.get("executed"):
        print(f"[!] Transaction not found or not executed")
        return
    
    actions = data.get("actions", [])
    print(f"╔═══════════════════════════════════════════════════════")
    print(f"║ Transaction: {trx_id[:48]}...")
    print(f"║ Actions: {len(actions)}")
    print(f"╚═══════════════════════════════════════════════════════\n")
    
    for i, action in enumerate(actions, 1):
        decoded = decode_action(action)
        print(f"[Action {i}/{len(actions)}]")
        print(format_decoded(decoded))
        print()

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Track Upland Appchain activity")
    parser.add_argument("-a", "--account", help="Filter by account name")
    parser.add_argument("-f", "--filter", help="Filter by code:action (e.g. upxtokenacct:transfer)")
    parser.add_argument("-i", "--interval", type=int, default=5, help="Poll interval in seconds")
    parser.add_argument("--dump", action="store_true", help="Dump recent actions and exit")
    parser.add_argument("-n", "--limit", type=int, default=100, help="Limit for dump mode")
    parser.add_argument("--all", action="store_true", help="Show all actions including noise (payforcpu, onblock)")
    parser.add_argument("--raw", action="store_true", help="Show raw JSON instead of decoded")
    parser.add_argument("--no-expand", action="store_true", help="Don't fetch full transactions (faster but less detail)")
    parser.add_argument("-t", "--transaction", help="Inspect a specific transaction by ID")
    
    args = parser.parse_args()
    
    if args.transaction:
        inspect_transaction(args.transaction)
    elif args.dump:
        dump_recent(account=args.account, limit=args.limit, show_all=args.all, raw=args.raw)
    else:
        poll_actions(
            account=args.account,
            action_filter=args.filter,
            interval=args.interval,
            show_all=args.all,
            expand_tx=not args.no_expand
        )
