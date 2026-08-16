"""
UplandScope — Web Application

A neighborhood mapping and optimization tool for Upland players.
"""

import config  # noqa: F401 — must be first to load .env before neighborhood_map

from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, session, Response, stream_with_context

def _neighborhoods():
    from neighborhoods import search_neighborhoods, get_all_neighborhoods
    return search_neighborhoods, get_all_neighborhoods

def _map_service():
    from map_service import request_map, get_job, get_cached_map, MAPS_DIR
    return request_map, get_job, get_cached_map, MAPS_DIR

def _optimizer():
    from collection_optimizer import load_collections, load_user_properties, optimize_collections
    return load_collections, load_user_properties, optimize_collections

def _tracker():
    from collection_tracker import analyze_collections
    return analyze_collections

def _forsale():
    from forsale_finder import find_forsale_for_collection
    return find_forsale_for_collection

def _score():
    from score_calculator import get_neighborhood_score, list_cached_neighborhoods
    return get_neighborhood_score, list_cached_neighborhoods

def _portfolio():
    from portfolio_analyzer import build_portfolio
    return build_portfolio

def _valuation():
    from valuation import estimate_value
    return estimate_value

def _valuation_batch():
    from valuation import estimate_batch, MAX_BATCH_ITEMS
    return estimate_batch, MAX_BATCH_ITEMS

def _floor_price():
    from floor_price import get_floor_prices
    return get_floor_prices

def _report():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "optimizer"))
    from recommender import generate_report
    return generate_report

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/neighborhoods")
def api_neighborhoods():
    search_neighborhoods, _ = _neighborhoods()
    q = request.args.get("q", "").strip()
    results = search_neighborhoods(q)
    return jsonify([
        {"name": h["name"], "city": h["city_name"], "id": h["id"]}
        for h in results
    ])


@app.route("/generate", methods=["POST"])
def generate():
    request_map, get_job, get_cached_map, MAPS_DIR = _map_service()
    neighborhood = request.form.get("neighborhood", "").strip()
    city_hint = request.form.get("city_hint", "").strip() or None
    username = request.form.get("username", "").strip()
    eos_account = request.form.get("eos_account", "").strip()
    mode = request.form.get("mode", "simple")
    show_zones = bool(request.form.get("zones"))

    if not neighborhood:
        return render_template("index.html", error="Please enter a neighborhood name.")

    if mode not in ("simple", "optimize"):
        mode = "simple"

    key = request_map(neighborhood, city_hint, username, eos_account, mode, show_zones)
    job = get_job(key)

    if job and job.get("status") == "ready":
        return redirect(url_for("map_view", key=key))

    return render_template("generating.html",
                           key=key,
                           neighborhood=neighborhood,
                           username=username,
                           mode=mode)


@app.route("/status/<key>")
def status(key):
    _, get_job, _, _ = _map_service()
    job = get_job(key)
    if not job:
        return jsonify({"status": "unknown"})
    resp = {
        "status": job.get("status", "unknown"),
        "progress": job.get("progress", ""),
    }
    if job.get("status") == "ready":
        resp["url"] = url_for("map_view", key=key)
    if job.get("error"):
        resp["error"] = job["error"]
    return jsonify(resp)


@app.route("/map/<key>")
def map_view(key):
    _, get_job, _, _ = _map_service()
    job = get_job(key)
    if not job or job.get("status") != "ready":
        return redirect(url_for("index"))
    return render_template("map_view.html", key=key)


@app.route("/maps/<path:filename>")
def serve_map(filename):
    _, _, _, MAPS_DIR = _map_service()
    return send_from_directory(str(MAPS_DIR), filename)


# ── Collection Optimizer ───────────────────────────────────────────────────

@app.route("/optimizer")
def optimizer():
    return render_template("optimizer.html")


@app.route("/optimizer/run", methods=["POST"])
def optimizer_run():
    load_collections, load_user_properties, optimize_collections = _optimizer()
    username = request.form.get("username", "").strip()
    eos_account = request.form.get("eos_account", "").strip()

    if not username or not eos_account:
        return render_template("optimizer.html", error="Both username and EOS account are required.")

    try:
        annual_rate_pct = request.form.get("annual_rate", "12.25").strip()
        try:
            annual_rate = float(annual_rate_pct) / 100.0
        except ValueError:
            annual_rate = 0.1225

        colls = load_collections()
        props = load_user_properties(username, eos_account)
        if not props:
            return render_template("optimizer.html", error=f"No properties found for {username}.")
        result = optimize_collections(props, colls, annual_rate=annual_rate)
        result["annual_rate_pct"] = round(annual_rate * 100, 2)
        return render_template("optimizer_results.html", result=result, username=username)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return render_template("optimizer.html", error=f"Error: {e}")


# ── Collection Tracker ─────────────────────────────────────────────────────

@app.route("/collections")
def collections():
    return render_template("collections.html")


@app.route("/collections/run", methods=["POST"])
def collections_run():
    load_collections, load_user_properties, _ = _optimizer()
    analyze_collections = _tracker()
    username = request.form.get("username", "").strip()
    eos_account = request.form.get("eos_account", "").strip()

    if not username or not eos_account:
        return render_template("collections.html", error="Both username and EOS account are required.")

    try:
        annual_rate_pct = request.form.get("annual_rate", "12.25").strip()
        try:
            annual_rate = float(annual_rate_pct) / 100.0
        except ValueError:
            annual_rate = 0.1225

        colls = load_collections()
        props = load_user_properties(username, eos_account)
        if not props:
            return render_template("collections.html", error=f"No properties found for {username}.")
        result = analyze_collections(props, colls)
        result["total_properties"] = len(props)

        # Portfolio-level yield baseline, so per-collection completion impact
        # (added in the template/JS) can be framed as a delta off a known total.
        total_mint_all = round(sum(p.get("mintPrice") or 0 for p in props))
        current_monthly_yield = round(total_mint_all * annual_rate / 12)
        result["total_mint_all"] = total_mint_all
        result["annual_rate"] = annual_rate
        result["annual_rate_pct"] = round(annual_rate * 100, 2)
        result["current_monthly_yield"] = current_monthly_yield
        result["current_hourly_yield"] = round(current_monthly_yield / (30 * 24), 2)

        # Store analysis in session so the forsale endpoint can use it without re-running
        session["coll_analysis"] = {
            "user_prop_ids": [p["id"] for p in props],
            "almost": result["almost"],
            "completable": result["completable"],
            "annual_rate": annual_rate,
        }

        return render_template("collections_results.html", result=result, username=username)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return render_template("collections.html", error=f"Error: {e}")


@app.route("/api/collections/forsale")
def api_collections_forsale():
    coll_id = request.args.get("coll_id", type=int)
    if not coll_id:
        return jsonify({"error": "coll_id required"}), 400

    analysis = session.get("coll_analysis")
    if not analysis:
        return jsonify({"error": "No active session — run analysis first"}), 400

    user_prop_ids = set(str(x) for x in analysis.get("user_prop_ids", []))
    all_entries = analysis.get("almost", []) + analysis.get("completable", [])
    coll_entry = next((c for c in all_entries if c["id"] == coll_id), None)

    if not coll_entry:
        return jsonify({"error": "Collection not found in session"}), 404

    try:
        find_forsale_for_collection = _forsale()
        listings = find_forsale_for_collection(coll_entry, user_prop_ids)
        # Cache listings on the session so the budget optimizer can reuse them
        cached = session.get("coll_listings") or {}
        cached[str(coll_id)] = listings
        session["coll_listings"] = cached
        return jsonify({"listings": listings, "count": len(listings)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/collections/budget-optimize")
def api_collections_budget_optimize():
    """
    Multi-collection knapsack: given a UPX budget, pick the combination of
    near-complete collections (with known for-sale listings) that maximizes
    projected monthly yield gain.

    Query params:
      budget — UPX budget (required)
      fetch  — if "true", auto-fetch for-sale listings for all almost-complete
               collections that aren't already cached in the session (slower)
    """
    budget_raw = request.args.get("budget", "").strip()
    try:
        budget = float(budget_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "budget (UPX number) required"}), 400
    if budget <= 0:
        return jsonify({"error": "budget must be > 0"}), 400

    analysis = session.get("coll_analysis")
    if not analysis:
        return jsonify({"error": "No active session — run analysis first"}), 400

    almost = analysis.get("almost") or []
    annual_rate = analysis.get("annual_rate") or 0.1225
    listings_by_id = dict(session.get("coll_listings") or {})
    user_prop_ids = set(str(x) for x in analysis.get("user_prop_ids", []))

    auto_fetch = request.args.get("fetch", "false").lower() == "true"
    if auto_fetch:
        find_forsale_for_collection = _forsale()
        for coll in almost:
            cid = str(coll["id"])
            if cid in listings_by_id:
                continue
            try:
                listings_by_id[cid] = find_forsale_for_collection(coll, user_prop_ids)
            except Exception:
                listings_by_id[cid] = []
        session["coll_listings"] = listings_by_id

    from multi_collection_optimizer import build_options_from_almost, optimize_budget
    # keys may be str or int depending on session serialization
    normalized = {}
    for k, v in listings_by_id.items():
        try:
            normalized[int(k)] = v
        except (TypeError, ValueError):
            normalized[k] = v

    options = build_options_from_almost(almost, normalized, annual_rate)
    result = optimize_budget(options, budget)
    result["options"] = [
        {
            "id": o["id"],
            "name": o["name"],
            "boost": o["boost"],
            "cost_upx": o["cost_upx"],
            "monthly_yield_gain": o["monthly_yield_gain"],
            "payback_days": o["payback_days"],
            "efficiency": o["efficiency"],
            "partial": o["partial"],
            "gap": o["gap"],
        }
        for o in options[:20]
    ]
    result["listings_cached"] = len(listings_by_id)
    result["almost_count"] = len(almost)
    return jsonify(result)


# ── Portfolio Analyzer ─────────────────────────────────────────────────────

@app.route("/portfolio")
def portfolio():
    return render_template("portfolio.html")


@app.route("/portfolio/run", methods=["POST"])
def portfolio_run():
    build_portfolio = _portfolio()
    username = request.form.get("username", "").strip()
    eos_account = request.form.get("eos_account", "").strip() or None

    if not username:
        return render_template("portfolio.html", error="Username is required.")

    annual_rate_pct = request.form.get("annual_rate", "12.25").strip()
    try:
        annual_rate = float(annual_rate_pct) / 100.0
    except ValueError:
        annual_rate = 0.1225
    estimate_market_value = request.form.get("estimate_market_value") == "on"

    try:
        result = build_portfolio(username, eos_account, annual_rate=annual_rate,
                                  estimate_market_value=estimate_market_value)
        if result.get("error"):
            return render_template("portfolio.html", error=result["error"])
        return render_template("portfolio_results.html", result=result, username=username)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return render_template("portfolio.html", error=f"Error: {e}")


# ── Property Valuation Tool ─────────────────────────────────────────────────

@app.route("/valuation")
def valuation():
    return render_template("valuation.html")


@app.route("/valuation/run", methods=["POST"])
def valuation_run():
    estimate_value = _valuation()
    query = request.form.get("query", "").strip()

    if not query:
        return render_template("valuation.html", error="Enter a property address or ID.")

    try:
        result = estimate_value(query)
        if result.get("error"):
            return render_template("valuation.html", error=result["error"], query=query)
        if result.get("matches"):
            return render_template("valuation.html", matches=result["matches"], query=query)
        return render_template("valuation_results.html", result=result, query=query)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return render_template("valuation.html", error=f"Error: {e}", query=query)


@app.route("/valuation/batch")
def valuation_batch():
    return render_template("valuation_batch.html")


@app.route("/valuation/batch/run", methods=["POST"])
def valuation_batch_run():
    estimate_batch, max_batch_items = _valuation_batch()
    raw = request.form.get("queries", "")
    queries = [line.strip() for line in raw.splitlines() if line.strip()]

    if not queries:
        return render_template("valuation_batch.html", error="Enter at least one address or property ID.")

    truncated = len(queries) > max_batch_items
    try:
        results = estimate_batch(queries)
        return render_template("valuation_batch_results.html", results=results,
                                truncated=truncated, max_batch_items=max_batch_items,
                                raw_queries=raw)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return render_template("valuation_batch.html", error=f"Error: {e}", raw_queries=raw)


@app.route("/floor")
def floor_price():
    neighborhood = request.args.get("neighborhood", "").strip()
    if not neighborhood:
        return render_template("floor_price.html")
    get_floor_prices = _floor_price()
    try:
        result = get_floor_prices(neighborhood)
        if result.get("error"):
            return render_template("floor_price.html", error=result["error"], neighborhood=neighborhood)
        return render_template("floor_price_results.html", result=result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return render_template("floor_price.html", error=f"Error: {e}", neighborhood=neighborhood)


@app.route("/floor/run", methods=["POST"])
def floor_price_run():
    get_floor_prices = _floor_price()
    neighborhood = request.form.get("neighborhood", "").strip()

    if not neighborhood:
        return render_template("floor_price.html", error="Enter a neighborhood name.")

    try:
        result = get_floor_prices(neighborhood)
        if result.get("error"):
            return render_template("floor_price.html", error=result["error"], neighborhood=neighborhood)
        return render_template("floor_price_results.html", result=result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return render_template("floor_price.html", error=f"Error: {e}", neighborhood=neighborhood)


# ── Neighborhood Score Dashboard ───────────────────────────────────────────

@app.route("/score")
def score():
    get_neighborhood_score, list_cached_neighborhoods = _score()
    hood = request.args.get("neighborhood", "Dongan Hills").strip()
    cached = list_cached_neighborhoods()
    score = get_neighborhood_score(hood)
    return render_template("score.html", score=score, neighborhood=hood, cached=cached)


@app.route("/api/score/report")
def score_report():
    hood = request.args.get("neighborhood", "Dongan Hills").strip()
    mine_only = request.args.get("mine_only", "true").lower() == "true"
    generate_report = _report()
    hood_key = hood.replace(" ", "_")
    result = generate_report(hood_key)
    rows = result.get("rows", []) if isinstance(result, dict) else (result or [])
    lu_balance = result.get("lu_balance") if isinstance(result, dict) else None
    spark_summary = result.get("spark_summary") if isinstance(result, dict) else None
    plan_progress = result.get("plan_progress") if isinstance(result, dict) else None
    commerce = result.get("commerce") if isinstance(result, dict) else None
    if mine_only:
        rows = [r for r in rows if r["is_mine"]]
        # Recompute summaries for the filtered set so mine_only=true stays accurate
        try:
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "optimizer"))
            from spark_estimator import summarize_queue
            from recommender import compute_plan_progress
            spark_summary = summarize_queue(rows, mine_only=False)
            plan_progress = compute_plan_progress(rows, mine_only=False)
        except Exception:
            pass
    return jsonify({
        "rows": rows,
        "neighborhood": hood,
        "total": len(rows),
        "lu_balance": lu_balance,
        "spark_summary": spark_summary,
        "plan_progress": plan_progress,
        "commerce": commerce,
    })


@app.route("/api/score/forsale")
def score_forsale():
    import sys, json, time, concurrent.futures, requests as _req
    from pathlib import Path
    hood = request.args.get("neighborhood", "Dongan Hills").strip()
    hood_key = hood.replace(" ", "_")

    optimizer_cache = Path(__file__).resolve().parent.parent / "optimizer" / "cache"
    props_path = optimizer_cache / f"{hood_key}_props_cache.json"
    if not props_path.exists():
        return jsonify({"listings": [], "error": "No cached data for this neighborhood"})

    props = json.loads(props_path.read_text())
    forsale_ids = {str(p["id"]) for p in props if p.get("status") == "For sale"}
    if not forsale_ids:
        return jsonify({"listings": []})

    # Cache live prices for 30 min
    price_cache_path = optimizer_cache / f"{hood_key}_forsale_prices.json"
    price_cache = {}
    if price_cache_path.exists() and (time.time() - price_cache_path.stat().st_mtime) < 1800:
        price_cache = json.loads(price_cache_path.read_text())

    to_fetch = [pid for pid in forsale_ids if pid not in price_cache]
    if to_fetch:
        def _fetch_price(pid):
            try:
                r = _req.get(f"https://api.upland.me/properties/{pid}",
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                if r.status_code == 200:
                    d = r.json()
                    om = d.get("on_market") or {}
                    fiat_raw = om.get("fiat", "0 FIAT")
                    try:
                        usd = float(fiat_raw.split()[0])
                    except (ValueError, IndexError):
                        usd = 0.0
                    return pid, {"price_upx": d.get("price"), "price_usd": usd or None,
                                 "on_market": bool(om)}
            except Exception:
                pass
            return pid, {"price_upx": None, "price_usd": None, "on_market": False}

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for pid, info in ex.map(_fetch_price, to_fetch):
                price_cache[pid] = info
        price_cache_path.write_text(json.dumps(price_cache))

    # Get recommendations for all props, cross-ref with for-sale
    generate_report = _report()
    result = generate_report(hood_key)
    rows = result.get("rows", []) if isinstance(result, dict) else (result or [])
    by_id = {r["prop_id"]: r for r in rows}

    listings = []
    for prop in props:
        pid = str(prop["id"])
        if pid not in forsale_ids:
            continue
        prices = price_cache.get(pid, {})
        if not prices.get("on_market", True) and prices.get("price_upx") is None:
            continue  # skip if we know it's no longer listed
        rec = by_id.get(pid, {})
        su_gain = rec.get("su_gain") or 0
        price_upx = prices.get("price_upx")
        upx_per_su = round(price_upx / su_gain, 1) if (price_upx and su_gain > 0) else None
        listings.append({
            "prop_id": pid,
            "address": prop.get("address", ""),
            "mint_price": prop.get("mintPrice"),
            "price_upx": price_upx,
            "price_usd": prices.get("price_usd"),
            "up2": rec.get("up2"),
            "eff_width": rec.get("eff_width"),
            "action": rec.get("action", ""),
            "recommended_name": rec.get("recommended_name", ""),
            "su_gain": su_gain,
            "su_cat": rec.get("su_cat", ""),
            "upx_per_su": upx_per_su,
        })

    listings.sort(key=lambda x: (x["su_gain"] == 0, -(x["su_gain"] or 0),
                                  x["upx_per_su"] is None, x["upx_per_su"] or 0))
    return jsonify({"listings": listings, "neighborhood": hood})


# ── Economy Dashboard ──────────────────────────────────────────────────────

import json
import time
import economy as _econ

_VALID_PERIODS = {"today", "7d", "30d", "90d", "all"}


@app.route("/economy")
def economy_dashboard():
    return render_template("economy.html")


@app.route("/api/economy/summary")
def api_economy_summary():
    period = request.args.get("period", "30d")
    if period not in _VALID_PERIODS:
        period = "30d"
    return jsonify(_econ.summary(period))


@app.route("/api/economy/timeseries")
def api_economy_timeseries():
    period = request.args.get("period", "30d")
    if period not in _VALID_PERIODS:
        period = "30d"
    return jsonify(_econ.timeseries(period))


@app.route("/api/economy/feed")
def api_economy_feed():
    limit = min(request.args.get("limit", 50, type=int), 200)
    marketplace = request.args.get("marketplace", None)
    city = request.args.get("city", None) or None
    last_id = request.args.get("since_id", 0, type=int)
    if last_id:
        rows = _econ.latest_since(last_id, city=city)
    else:
        rows = _econ.feed(limit, marketplace, city=city)
    return jsonify({"transactions": rows, "max_id": _econ.max_id()})


@app.route("/api/economy/cities")
def api_economy_cities():
    period = request.args.get("period", "30d")
    if period not in _VALID_PERIODS:
        period = "30d"
    return jsonify({"cities": _econ.cities(period)})


@app.route("/api/economy/whales")
def api_economy_whales():
    from username_lookup import lookup_many
    period = request.args.get("period", "30d")
    if period not in _VALID_PERIODS:
        period = "30d"
    data = _econ.whales(period)
    # Resolve EOS accounts → Upland usernames
    all_accounts = (
        [r["account"] for r in data.get("buyers", []) if r.get("account")]
        + [r["account"] for r in data.get("sellers", []) if r.get("account")]
    )
    names = lookup_many(all_accounts)
    for r in data.get("buyers", []) + data.get("sellers", []):
        r["username"] = names.get(r.get("account"))
    return jsonify(data)


# ── Startup ────────────────────────────────────────────────────────────────

def preload_neighborhoods():
    try:
        _, get_all_neighborhoods = _neighborhoods()
        get_all_neighborhoods()
    except Exception as e:
        print(f"[!] Failed to preload neighborhoods: {e}")


if __name__ == "__main__":
    import threading
    threading.Thread(target=preload_neighborhoods, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=True)
