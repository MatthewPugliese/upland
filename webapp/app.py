"""
UplandScope — Web Application

A neighborhood mapping and optimization tool for Upland players.
"""

import config  # noqa: F401 — must be first to load .env before neighborhood_map

from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, session

from neighborhoods import search_neighborhoods, get_all_neighborhoods
from map_service import request_map, get_job, get_cached_map, MAPS_DIR
from collection_optimizer import load_collections, load_user_properties, optimize_collections
from collection_tracker import analyze_collections
from forsale_finder import find_forsale_for_collection

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/neighborhoods")
def api_neighborhoods():
    q = request.args.get("q", "").strip()
    results = search_neighborhoods(q)
    return jsonify([
        {"name": h["name"], "city": h["city_name"], "id": h["id"]}
        for h in results
    ])


@app.route("/generate", methods=["POST"])
def generate():
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
    job = get_job(key)
    if not job or job.get("status") != "ready":
        return redirect(url_for("index"))
    return render_template("map_view.html", key=key)


@app.route("/maps/<path:filename>")
def serve_map(filename):
    return send_from_directory(str(MAPS_DIR), filename)


# ── Collection Optimizer ───────────────────────────────────────────────────

@app.route("/optimizer")
def optimizer():
    return render_template("optimizer.html")


@app.route("/optimizer/run", methods=["POST"])
def optimizer_run():
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
        listings = find_forsale_for_collection(coll_entry, user_prop_ids)
        return jsonify({"listings": listings, "count": len(listings)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ── Startup ────────────────────────────────────────────────────────────────

def preload_neighborhoods():
    """Cache neighborhood list on first request (lazy)."""
    try:
        get_all_neighborhoods()
    except Exception as e:
        print(f"[!] Failed to preload neighborhoods: {e}")


if __name__ == "__main__":
    import threading
    threading.Thread(target=preload_neighborhoods, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=True)
