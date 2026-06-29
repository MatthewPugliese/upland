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
        colls = load_collections()
        props = load_user_properties(username, eos_account)
        if not props:
            return render_template("collections.html", error=f"No properties found for {username}.")
        result = analyze_collections(props, colls)
        result["total_properties"] = len(props)

        # Store analysis in session so the forsale endpoint can use it without re-running
        session["coll_analysis"] = {
            "user_prop_ids": [p["id"] for p in props],
            "almost": result["almost"],
            "completable": result["completable"],
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
        return jsonify({"listings": listings, "count": len(listings)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


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
    # Normalize "Dongan Hills" → "Dongan_Hills" for file lookup
    hood_key = hood.replace(" ", "_")
    rows = generate_report(hood_key)
    if mine_only:
        rows = [r for r in rows if r["is_mine"]]
    return jsonify({"rows": rows, "neighborhood": hood, "total": len(rows)})


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
    period = request.args.get("period", "30d")
    if period not in _VALID_PERIODS:
        period = "30d"
    return jsonify(_econ.whales(period))


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
