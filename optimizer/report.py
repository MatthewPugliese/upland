#!/usr/bin/env python3
"""
Upland Neighborhood Recommendation Report

Generates a self-contained HTML table ranked by SU gain with zone/action filters
and sortable columns. All filtering and sorting runs client-side — no server needed.

Usage (standalone):
    python3 report.py "Dongan Hills" --username pugs08
    python3 report.py "Rosebank" --city "Staten Island"

Also called from zone_map.py after the map is rendered.
"""
import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from structure_fitter import STRUCTURES
from recommender import auto_recommend, compute_lu_balance

ZONE_COLORS = {
    "commercial":  "#E74C3C",
    "residential": "#3498DB",
    "public":      "#9B59B6",
    "mixed":       "#2ECC71",
    "industrial":  "#F39C12",
    "green":       "#1ABC9C",
}
ZONE_NAMES = {
    "commercial": "Commercial",
    "residential": "Residential",
    "public": "Public Services",
    "mixed": "Mixed Use",
    "industrial": "Industrial",
    "green": "Green / STEM",
}
ACTION_COLORS = {
    "BUILD":            "#F39C12",
    "DEMOLISH → BUILD": "#E74C3C",
    "KEEP":             "#2ECC71",
}


def build_rows(props, structures, user_ids, api_dims, prop_zones, neighborhood_counts, lu_balance):
    """
    Build recommendation row dicts for all properties.

    Returns list of dicts suitable for JSON serialization and HTML rendering.
    """
    lu_deficit = lu_balance["status"] in ("lu_deficit", "lu_critical")
    rows = []
    for prop in props:
        prop_id = str(prop.get("id", ""))
        address = prop.get("address", "")
        zone = prop_zones.get(prop_id, "mixed")
        structs = structures.get(prop_id, [])
        dims = api_dims.get(address.upper().strip()) or {}
        is_mine = prop_id in user_ids

        rec = auto_recommend(
            prop_id,
            dims.get("up2"),
            dims.get("eff_width", dims.get("width_up")),
            dims.get("depth_up"),
            structs,
            zone,
            neighborhood_counts,
            lu_deficit=lu_deficit and is_mine,
        )

        current_lu = sum(
            STRUCTURES.get(n, {}).get("living_units", 0)
            for n in rec["current_names"]
        )

        rows.append({
            "prop_id":          prop_id,
            "address":          address,
            "zone":             zone,
            "up2":              dims.get("up2"),
            "eff_width":        dims.get("eff_width") or dims.get("width_up"),
            "depth_up":         dims.get("depth_up"),
            "is_mine":          is_mine,
            "action":           rec["action"],
            "recommended_name": rec["recommended_name"] or "",
            "recommended_su":   rec["recommended_su"],
            "su_cat":           rec["su_cat"] or "",
            "su_gain":          rec["su_gain"] or 0,
            "current_names":    rec["current_names"],
            "current_su":       rec["current_su"],
            "current_lu":       current_lu,
            "addons":           rec["addons"],
            "desc":             rec["desc"],
        })

    rows.sort(key=lambda r: (
        0 if r["is_mine"] else 1,
        {"DEMOLISH → BUILD": 0, "BUILD": 1, "KEEP": 2}.get(r["action"], 9),
        -(r["su_gain"] or 0),
    ))
    return rows


def render_report(hood_name: str, rows: list, lu_balance: dict,
                  neighborhood_counts: dict, username: str, output_path: Path) -> None:
    """Write a self-contained HTML recommendation report to output_path."""

    mine_rows = [r for r in rows if r["is_mine"]]
    total_current_su = round(sum(r["current_su"] for r in mine_rows))
    total_su_gain    = round(sum(r["su_gain"] for r in mine_rows))
    total_potential  = total_current_su + total_su_gain
    n_build          = sum(1 for r in mine_rows if r["action"] == "BUILD")
    n_demolish       = sum(1 for r in mine_rows if r["action"] == "DEMOLISH → BUILD")
    n_keep           = sum(1 for r in mine_rows if r["action"] == "KEEP")

    lu_color = {"balanced": "#27AE60", "su_deficit": "#F39C12",
                "lu_deficit": "#E74C3C", "lu_critical": "#C0392B"}.get(
                    lu_balance["status"], "#888")

    # Zones present across all rows
    zones_all = sorted({r["zone"] for r in rows if r["zone"] in ZONE_COLORS})

    zone_btn_html = '<button class="zbtn active" data-zone="all" onclick="setZone(this,\'all\')">All</button>'
    for z in zones_all:
        zone_btn_html += (
            f'<button class="zbtn" data-zone="{z}" onclick="setZone(this,\'{z}\')" '
            f'style="--zc:{ZONE_COLORS[z]}">{ZONE_NAMES.get(z, z)}</button>'
        )

    data_json = json.dumps(rows, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{hood_name} — Recommendation Report</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Arial,sans-serif;background:#f0f2f5;padding:16px;color:#222}}
h1{{font-size:20px;font-weight:700}}
.sub{{font-size:13px;color:#888;margin-top:2px}}

.card{{background:white;border-radius:10px;box-shadow:0 1px 6px rgba(0,0,0,.1);padding:16px 20px;margin-bottom:12px}}

.metrics{{display:flex;gap:24px;margin-top:12px;flex-wrap:wrap}}
.metric .val{{font-size:26px;font-weight:700}}
.metric .lbl{{font-size:11px;color:#888;margin-top:2px}}
.metric.gain .val{{color:#27AE60}}
.metric.warn .val{{color:#E74C3C}}

.lu-bar{{margin-top:12px;padding-top:10px;border-top:1px solid #f0f0f0;display:flex;align-items:center;gap:10px;font-size:12px}}
.lu-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0;background:{lu_color}}}

.filters{{display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
.filter-section{{display:flex;gap:4px;align-items:center;flex-wrap:wrap}}
.filter-label{{font-size:11px;color:#888;margin-right:4px;white-space:nowrap}}

button{{cursor:pointer;border:1px solid #ddd;background:white;border-radius:12px;
        padding:4px 11px;font-size:12px;transition:all .15s}}
button:hover{{background:#f5f5f5}}
.zbtn.active{{background:var(--zc,#555);color:white;border-color:transparent}}
.zbtn[data-zone="all"].active{{background:#555}}
.abtn.active{{color:white;border-color:transparent}}
.abtn[data-action="BUILD"].active{{background:#F39C12}}
.abtn[data-action="DEMOLISH → BUILD"].active{{background:#E74C3C}}
.abtn[data-action="KEEP"].active{{background:#2ECC71}}
.abtn[data-action="all"].active{{background:#555}}

label{{font-size:12px;color:#444;display:flex;align-items:center;gap:5px;cursor:pointer}}
input[type=number]{{width:54px;padding:3px 6px;border:1px solid #ddd;border-radius:6px;font-size:12px}}
input[type=checkbox]{{cursor:pointer}}

.tbl-wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
thead th{{padding:9px 10px;text-align:left;background:#fafafa;border-bottom:2px solid #eee;
          font-size:11px;color:#777;cursor:pointer;white-space:nowrap;user-select:none}}
thead th:hover{{background:#f0f0f0}}
thead th.sort-asc::after{{content:" ↑"}}
thead th.sort-desc::after{{content:" ↓"}}
tbody tr:hover td{{background:#fafafa}}
tbody tr.mine td{{}}
tbody tr:not(.mine) td{{color:#aaa}}
td{{padding:7px 10px;border-bottom:1px solid #f5f5f5;vertical-align:top}}
td.addr{{font-weight:600;color:#222;white-space:nowrap}}
tr:not(.mine) td.addr{{font-weight:400;color:#bbb}}
.badge{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;
        font-weight:600;color:white;white-space:nowrap}}
.su-gain{{font-size:15px;font-weight:700}}
.su-gain.high{{color:#27AE60}}
.su-gain.med{{color:#F39C12}}
.su-gain.low{{color:#E74C3C}}
.su-gain.zero{{color:#ccc}}
.current{{font-size:11px;color:#888}}
.addons{{font-size:10px;color:#aaa;margin-top:2px}}
.empty-msg{{padding:32px;text-align:center;color:#aaa;font-size:14px}}
.count-bar{{font-size:12px;color:#888;margin-top:8px}}
</style>
</head>
<body>

<div class="card">
  <h1>{hood_name} — Recommendation Report</h1>
  <div class="sub">{f"Owned by {username}" if username else "All properties"}</div>
  <div class="metrics">
    <div class="metric gain">
      <div class="val" id="mTotalGain">+{total_su_gain}</div>
      <div class="lbl">Total SU Gain (all actions)</div>
    </div>
    <div class="metric">
      <div class="val">{total_current_su}</div>
      <div class="lbl">Current SU (owned)</div>
    </div>
    <div class="metric">
      <div class="val">{total_potential}</div>
      <div class="lbl">Potential SU</div>
    </div>
    <div class="metric">
      <div class="val" style="color:#F39C12">{n_build}</div>
      <div class="lbl">Build actions</div>
    </div>
    <div class="metric">
      <div class="val" style="color:#E74C3C">{n_demolish}</div>
      <div class="lbl">Demolish &amp; rebuild</div>
    </div>
    <div class="metric">
      <div class="val" style="color:#2ECC71">{n_keep}</div>
      <div class="lbl">Already optimal</div>
    </div>
  </div>
  <div class="lu-bar">
    <div class="lu-dot"></div>
    <span><b>SU/LU:</b> {lu_balance["message"]}</span>
  </div>
</div>

<div class="card filters">
  <div class="filter-section">
    <span class="filter-label">Zone</span>
    {zone_btn_html}
  </div>
  <div class="filter-section">
    <span class="filter-label">Action</span>
    <button class="abtn active" data-action="all" onclick="setAction(this,'all')">All</button>
    <button class="abtn" data-action="BUILD" onclick="setAction(this,'BUILD')">Build</button>
    <button class="abtn" data-action="DEMOLISH → BUILD" onclick="setAction(this,'DEMOLISH → BUILD')">Demolish</button>
    <button class="abtn" data-action="KEEP" onclick="setAction(this,'KEEP')">Keep</button>
  </div>
  <label><input type="checkbox" id="mineOnly" {"checked" if username else ""}> Mine only</label>
  <label>Min SU gain <input type="number" id="minGain" value="0" min="0" max="99"></label>
</div>

<div class="card" style="padding:0;overflow:hidden">
  <div class="tbl-wrap">
  <table>
    <thead>
      <tr>
        <th onclick="sortBy('address')">Address</th>
        <th onclick="sortBy('zone')">Zone</th>
        <th onclick="sortBy('up2')">UP²</th>
        <th onclick="sortBy('eff_width')">Width</th>
        <th onclick="sortBy('action')">Action</th>
        <th onclick="sortBy('recommended_name')">Recommendation</th>
        <th onclick="sortBy('su_cat')">SU Type</th>
        <th onclick="sortBy('su_gain')">SU Gain ↓</th>
        <th onclick="sortBy('current_su')">Current</th>
      </tr>
    </thead>
    <tbody id="tbody"><tr><td colspan="9" class="empty-msg">Loading…</td></tr></tbody>
  </table>
  </div>
  <div class="count-bar" id="countBar" style="padding:8px 16px"></div>
</div>

<script>
const DATA = {data_json};

const ZONE_COLORS = {json.dumps(ZONE_COLORS)};
const ZONE_NAMES  = {json.dumps(ZONE_NAMES)};
const ACTION_COLORS = {json.dumps(ACTION_COLORS)};

let state = {{
  zone: 'all', action: 'all',
  mineOnly: {'true' if username else 'false'},
  minGain: 0,
  sortCol: 'su_gain', sortDir: -1
}};

function setZone(btn, z) {{
  document.querySelectorAll('.zbtn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  state.zone = z; render();
}}
function setAction(btn, a) {{
  document.querySelectorAll('.abtn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  state.action = a; render();
}}
document.getElementById('mineOnly').addEventListener('change', e => {{ state.mineOnly = e.target.checked; render(); }});
document.getElementById('minGain').addEventListener('input', e => {{ state.minGain = parseInt(e.target.value)||0; render(); }});

function sortBy(col) {{
  if (state.sortCol === col) state.sortDir *= -1;
  else {{ state.sortCol = col; state.sortDir = col === 'su_gain' ? -1 : 1; }}
  document.querySelectorAll('thead th').forEach(th => th.classList.remove('sort-asc','sort-desc'));
  const ths = document.querySelectorAll('thead th');
  const cols = ['address','zone','up2','eff_width','action','recommended_name','su_cat','su_gain','current_su'];
  const idx = cols.indexOf(col);
  if (idx >= 0) ths[idx].classList.add(state.sortDir === 1 ? 'sort-asc' : 'sort-desc');
  render();
}}

function filtered() {{
  return DATA.filter(r => {{
    if (state.mineOnly && !r.is_mine) return false;
    if (state.zone !== 'all' && r.zone !== state.zone) return false;
    if (state.action !== 'all' && r.action !== state.action) return false;
    if (r.su_gain < state.minGain) return false;
    return true;
  }});
}}

function render() {{
  const rows = filtered();
  rows.sort((a,b) => {{
    let av = a[state.sortCol], bv = b[state.sortCol];
    if (av == null) av = ''; if (bv == null) bv = '';
    if (typeof av === 'string') return av.localeCompare(bv) * state.sortDir;
    return (av - bv) * state.sortDir;
  }});

  const visGain = Math.round(rows.reduce((s,r) => s + (r.su_gain||0), 0));
  document.getElementById('mTotalGain').textContent = '+' + visGain;
  document.getElementById('countBar').textContent =
    rows.length + ' properties shown' +
    (visGain ? ' · +' + visGain + ' SU gain' : '');

  if (!rows.length) {{
    document.getElementById('tbody').innerHTML =
      '<tr><td colspan="9" class="empty-msg">No properties match the current filters.</td></tr>';
    return;
  }}

  const html = rows.map(r => {{
    const zcolor = ZONE_COLORS[r.zone] || '#888';
    const zname  = ZONE_NAMES[r.zone]  || r.zone;
    const acolor = ACTION_COLORS[r.action] || '#888';

    const gainVal = Math.round(r.su_gain || 0);
    const gainClass = gainVal >= 10 ? 'high' : gainVal >= 5 ? 'med' : gainVal > 0 ? 'low' : 'zero';
    const gainStr = gainVal > 0 ? '+' + gainVal : gainVal === 0 ? '—' : gainVal;

    const currentStr = r.current_names.length
      ? r.current_names.join(', ') + ' <span style="color:#bbb">(' + Math.round(r.current_su) + ' SU)</span>'
      : '<span style="color:#ddd">Empty</span>';

    const addonsStr = r.addons.length
      ? '<div class="addons">+ ' + r.addons.join(', ') + '</div>'
      : '';

    const recStr = r.recommended_name
      ? r.recommended_name + (r.recommended_su ? ' <span style="color:#aaa;font-size:11px">(' + Math.round(r.recommended_su) + ' SU)</span>' : '')
      : '<span style="color:#ddd">—</span>';

    const widthStr = r.eff_width != null ? r.eff_width.toFixed(1) + '^' : '—';
    const up2Str   = r.up2 != null ? Math.round(r.up2) : '—';

    return '<tr class="' + (r.is_mine ? 'mine' : '') + '">' +
      '<td class="addr">' + r.address + '</td>' +
      '<td><span class="badge" style="background:' + zcolor + '">' + zname + '</span></td>' +
      '<td>' + up2Str + '</td>' +
      '<td>' + widthStr + '</td>' +
      '<td><span class="badge" style="background:' + acolor + '">' + r.action + '</span></td>' +
      '<td>' + recStr + addonsStr + '</td>' +
      '<td style="color:#888;font-size:11px">' + (r.su_cat || '—') + '</td>' +
      '<td><span class="su-gain ' + gainClass + '">' + gainStr + '</span></td>' +
      '<td class="current">' + currentStr + '</td>' +
      '</tr>';
  }}).join('');

  document.getElementById('tbody').innerHTML = html;
}}

render();
</script>
</body>
</html>"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"[+] Report saved → {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate Upland neighborhood recommendation report")
    parser.add_argument("neighborhood")
    parser.add_argument("--city", default="")
    parser.add_argument("--username", default="pugs08")
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()

    safe_name = "".join(
        c if c.isalnum() or c in " -_" else "_" for c in args.neighborhood
    ).strip().replace(" ", "_")

    cache_dir  = SCRIPT_DIR / "cache"
    output_dir = Path(args.output_dir)

    props_path    = cache_dir / f"{safe_name}_props_cache.json"
    structs_path  = cache_dir / f"{safe_name}_structures_cache.json"
    dims_path     = cache_dir / f"{safe_name}_api_dims_cache.json"
    zones_path    = cache_dir / f"{safe_name}_osm_zones_cache.json"
    blockchain_path = cache_dir / "pugs08_blockchain_cache.json"
    overrides_path  = cache_dir / f"{safe_name}_zone_overrides.json"
    report_path     = output_dir / f"{safe_name}_Report.html"

    if not props_path.exists():
        print(f"[!] No props cache for '{args.neighborhood}'. Run zone_map.py first.")
        sys.exit(1)

    props      = json.loads(props_path.read_text())
    structures = json.loads(structs_path.read_text()) if structs_path.exists() else {}
    dims_raw   = json.loads(dims_path.read_text()) if dims_path.exists() else {}
    api_dims   = {k: v for k, v in dims_raw.items() if k != "_ts"}
    blockchain = json.loads(blockchain_path.read_text()) if blockchain_path.exists() else {}
    user_ids   = {str(pid) for pid in blockchain.get("owned", [])}

    # Load OSM zones (v2 cache with zones key, or old flat format)
    street_zones: dict[str, str] = {}
    if zones_path.exists():
        raw = json.loads(zones_path.read_text())
        street_zones = raw.get("zones", raw) if isinstance(raw, dict) else {}

    # Apply overrides
    if overrides_path.exists():
        try:
            ov = json.loads(overrides_path.read_text())
            street_zones.update({k.upper().strip(): v for k, v in ov.items()})
        except Exception:
            pass

    # Assign zones to properties using OSM data
    from zone_map import assign_zone
    prop_zones = {
        str(p["id"]): assign_zone(p.get("address", ""), street_zones)
        for p in props
    }

    neighborhood_counts: dict[str, int] = {}
    for structs_list in structures.values():
        for s in structs_list:
            name = s.get("buildingName", "")
            if name:
                neighborhood_counts[name] = neighborhood_counts.get(name, 0) + 1

    lu_balance = compute_lu_balance(structures, user_ids=user_ids)
    rows = build_rows(props, structures, user_ids, api_dims, prop_zones,
                      neighborhood_counts, lu_balance)

    render_report(args.neighborhood, rows, lu_balance, neighborhood_counts,
                  args.username, report_path)


if __name__ == "__main__":
    main()
