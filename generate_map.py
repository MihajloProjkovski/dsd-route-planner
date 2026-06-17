#!/usr/bin/env python3
"""
generate_map.py
---------------
Reads _setup/customer_master.xlsx and produces customer_map.html --
a fully self-contained Leaflet.js map with zone + vehicle type filters.

Run:  python generate_map.py
"""

import json
import os
import sys
import pandas as pd
import config

# Zone colour palette - type-based so new zone names work automatically
TYPE_COLOURS = {
    "Kamion":  ["#1A5276", "#2E86C1", "#1F618D", "#2980B9",
                "#5DADE2", "#154360", "#7FB3D3", "#AED6F1"],
    "Furgon":  ["#1E8449", "#27AE60", "#1D8348", "#52BE80"],
    "Van":     ["#D35400", "#E67E22", "#CA6F1E", "#F39C12"],
    "Plostad": ["#E74C3C"],
    "Carsija": ["#8E44AD"],
}
DEFAULT_COLOUR = "#7F8C8D"

_type_colour_idx: dict = {}


def zone_colour(zone_name: str) -> str:
    for prefix in TYPE_COLOURS:
        if zone_name.startswith(prefix):
            palette = TYPE_COLOURS[prefix]
            idx = _type_colour_idx.get(zone_name)
            if idx is None:
                used = sum(1 for z in _type_colour_idx if z.startswith(prefix))
                idx  = used % len(palette)
                _type_colour_idx[zone_name] = idx
            return palette[idx]
    return DEFAULT_COLOUR


VEH_ICONS = {
    "Kamion": "🚛",
    "Furgon": "🚐",
    "Van":    "🚌",
}

SPECIAL_ZONE_POLYGONS = {
    name: zdef["polygon"]
    for name, zdef in config.SPECIAL_ZONES.items()
}


def load_customers(master_path):
    if not os.path.exists(master_path):
        sys.exit(f"ERROR: {master_path} not found. Run _setup/run_setup.bat first.")

    df = pd.read_excel(master_path, sheet_name="Customers")
    df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_")
    df["latitude"]  = pd.to_numeric(df["latitude"],  errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df = df.dropna(subset=["latitude", "longitude"]).copy()
    df["zone"]               = df["zone"].fillna("Unknown").astype(str)
    df["special_zone"]       = df["special_zone"].fillna("").astype(str)
    df["eligible_vehicles"]  = df["eligible_vehicles"].fillna("Van").astype(str)
    df["preferred_vehicle"]  = df["preferred_vehicle"].fillna("Van").astype(str)
    df["customer_name"]      = df["customer_name"].fillna("").astype(str)
    df["street"]             = df["street"].fillna("").astype(str)
    df["avg_weight_kg"]      = pd.to_numeric(df.get("avg_weight_kg", 0), errors="coerce").fillna(0)
    df["visits"]             = pd.to_numeric(df.get("visits", 0), errors="coerce").fillna(0).astype(int)
    return df


def build_geojson(df):
    features = []
    for _, row in df.iterrows():
        colour = zone_colour(row["zone"])
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [row["longitude"], row["latitude"]],
            },
            "properties": {
                "code":      str(row["customer_code"]),
                "name":      row["customer_name"],
                "street":    row["street"],
                "zone":      row["zone"],
                "special":   row["special_zone"],
                "eligible":  row["eligible_vehicles"],
                "preferred": row["preferred_vehicle"],
                "avg_kg":    round(float(row["avg_weight_kg"]), 1),
                "visits":    int(row["visits"]),
                "colour":    colour,
            },
        })
    return {"type": "FeatureCollection", "features": features}


def build_polygons(df):
    polys = []
    for zone_name, polygon in SPECIAL_ZONE_POLYGONS.items():
        colour = zone_colour(zone_name)
        coords = [[lon, lat] for lat, lon in polygon]
        polys.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [coords]},
            "properties": {"zone": zone_name, "colour": colour},
        })
    return {"type": "FeatureCollection", "features": polys}


def render_html(geojson, polygons, all_zones, output_path):
    zone_colours_dict = {z: zone_colour(z) for z in all_zones}
    for sz in SPECIAL_ZONE_POLYGONS:
        zone_colours_dict[sz] = zone_colour(sz)
    zone_colours_js  = json.dumps(zone_colours_dict, ensure_ascii=False)
    geojson_js       = json.dumps(geojson,   ensure_ascii=False)
    polygons_js      = json.dumps(polygons,  ensure_ascii=False)
    all_zones_js     = json.dumps(sorted(all_zones), ensure_ascii=False)
    veh_types_js     = json.dumps(["Kamion", "Furgon", "Van"])
    default_colour   = DEFAULT_COLOUR

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>DSD Customer Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; display: flex; height: 100vh; overflow: hidden; }}
  #sidebar {{ width: 280px; min-width: 220px; background: #1F2D3D; color: #ECF0F1; display: flex; flex-direction: column; overflow: hidden; box-shadow: 2px 0 8px rgba(0,0,0,.4); }}
  #sidebar h1 {{ font-size: 15px; font-weight: 700; padding: 14px 16px 10px; border-bottom: 1px solid #2C3E50; letter-spacing: .5px; color: #fff; }}
  #sidebar h2 {{ font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 1px; color: #95A5A6; padding: 10px 16px 4px; }}
  #filter-wrap {{ flex: 1; overflow-y: auto; padding-bottom: 12px; }}
  .filter-section {{ padding: 0 16px; }}
  .check-row {{ display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 13px; cursor: pointer; user-select: none; }}
  .check-row input {{ cursor: pointer; accent-color: #3498DB; width: 14px; height: 14px; }}
  .swatch {{ width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }}
  .btn-row {{ display: flex; gap: 6px; padding: 6px 16px; }}
  .btn {{ flex: 1; padding: 5px 0; font-size: 11px; font-weight: 600; border: none; border-radius: 4px; cursor: pointer; background: #2C3E50; color: #BDC3C7; transition: background .15s; }}
  .btn:hover {{ background: #3498DB; color: #fff; }}
  #stats {{ padding: 8px 16px; font-size: 12px; color: #7F8C8D; border-top: 1px solid #2C3E50; margin-top: 4px; }}
  #map {{ flex: 1; }}
  .lf-popup {{ font-size: 13px; line-height: 1.5; min-width: 210px; }}
  .lf-popup b {{ color: #2C3E50; }}
  .lf-popup .badge {{ display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px; font-weight: 600; color: #fff; margin: 2px 2px 0 0; }}
</style>
</head>
<body>
<div id="sidebar">
  <h1>🗺 DSD Customer Map</h1>
  <div id="filter-wrap">
    <h2>Vehicle Type</h2>
    <div class="filter-section" id="veh-filters"></div>
    <div class="btn-row">
      <button class="btn" onclick="toggleAll('veh', true)">All</button>
      <button class="btn" onclick="toggleAll('veh', false)">None</button>
    </div>
    <h2>Zone</h2>
    <div class="filter-section" id="zone-filters"></div>
    <div class="btn-row">
      <button class="btn" onclick="toggleAll('zone', true)">All</button>
      <button class="btn" onclick="toggleAll('zone', false)">None</button>
    </div>
  </div>
  <div id="stats">Showing <b id="vis-count">-</b> of <b id="tot-count">-</b> customers</div>
</div>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const GEOJSON      = {geojson_js};
const POLYGONS     = {polygons_js};
const ALL_ZONES    = {all_zones_js};
const VEH_TYPES    = {veh_types_js};
const ZONE_COLOURS = {zone_colours_js};
const DEFAULT_CLR  = "{default_colour}";

const map = L.map('map', {{ zoomControl: true }}).setView([41.998, 21.435], 13);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{ attribution: '&copy; OpenStreetMap contributors', maxZoom: 19 }}).addTo(map);

POLYGONS.features.forEach(f => {{
  const clr = f.properties.colour;
  L.geoJSON(f, {{ style: {{ color: clr, weight: 2, fillColor: clr, fillOpacity: 0.08 }} }}).bindTooltip(f.properties.zone, {{ sticky: true }}).addTo(map);
}});

const allMarkers = [];
function makeIcon(colour) {{
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14"><circle cx="7" cy="7" r="6" fill="${{colour}}" stroke="#fff" stroke-width="1.5"/></svg>`;
  return L.divIcon({{ html: svg, className: '', iconSize: [14, 14], iconAnchor: [7, 7] }});
}}

GEOJSON.features.forEach(f => {{
  const p = f.properties;
  const latlng = [f.geometry.coordinates[1], f.geometry.coordinates[0]];
  const marker = L.marker(latlng, {{ icon: makeIcon(p.colour) }});
  const eligible = p.eligible.split(',').map(v => v.trim());
  const badgeHtml = eligible.map(v => {{
    const colours = {{ Kamion: '#2980B9', Furgon: '#27AE60', Van: '#E67E22' }};
    return `<span class="badge" style="background:${{colours[v] || '#7F8C8D'}}">${{v}}</span>`;
  }}).join('');
  marker.bindPopup(`<div class="lf-popup"><b>${{p.name}}</b><br><span style="color:#7F8C8D;font-size:11px">${{p.code}}</span><br>${{p.street ? `<span style="font-size:12px">📍 ${{p.street}}</span><br>` : ''}}<br><b>Zone:</b> <span style="color:${{p.colour}};font-weight:600">${{p.zone}}</span><br><b>Vehicles:</b> ${{badgeHtml}}<br><b>Preferred:</b> ${{p.preferred}}<br><b>Avg weight:</b> ${{p.avg_kg.toLocaleString()}} kg<br><b>Visits:</b> ${{p.visits}}</div>`, {{ maxWidth: 260 }});
  marker._zone = p.zone;
  marker._eligible = eligible;
  allMarkers.push(marker);
}});

const markerGroup = L.layerGroup(allMarkers).addTo(map);
const activeZones = new Set(ALL_ZONES);
const activeVeh   = new Set(VEH_TYPES);

function applyFilters() {{
  let visible = 0;
  allMarkers.forEach(m => {{
    const zoneOk = activeZones.has(m._zone);
    const vehOk  = m._eligible.some(v => activeVeh.has(v));
    if (zoneOk && vehOk) {{ if (!map.hasLayer(m)) markerGroup.addLayer(m); visible++; }}
    else {{ if (map.hasLayer(m)) markerGroup.removeLayer(m); }}
  }});
  document.getElementById('vis-count').textContent = visible;
}}

const zoneWrap = document.getElementById('zone-filters');
ALL_ZONES.forEach(zone => {{
  const clr = ZONE_COLOURS[zone] || DEFAULT_CLR;
  const div = document.createElement('label');
  div.className = 'check-row';
  div.innerHTML = `<input type="checkbox" data-group="zone" data-val="${{zone}}" checked><span class="swatch" style="background:${{clr}}"></span><span>${{zone}}</span>`;
  div.querySelector('input').addEventListener('change', e => {{ e.target.checked ? activeZones.add(zone) : activeZones.delete(zone); applyFilters(); }});
  zoneWrap.appendChild(div);
}});

const vehClrs = {{ Kamion: '#2980B9', Furgon: '#27AE60', Van: '#E67E22' }};
const vehWrap = document.getElementById('veh-filters');
VEH_TYPES.forEach(vt => {{
  const div = document.createElement('label');
  div.className = 'check-row';
  div.innerHTML = `<input type="checkbox" data-group="veh" data-val="${{vt}}" checked><span class="swatch" style="background:${{vehClrs[vt]}}"></span><span>${{vt}}</span>`;
  div.querySelector('input').addEventListener('change', e => {{ e.target.checked ? activeVeh.add(vt) : activeVeh.delete(vt); applyFilters(); }});
  vehWrap.appendChild(div);
}});

function toggleAll(group, state) {{
  document.querySelectorAll(`input[data-group="${{group}}"]`).forEach(cb => {{
    cb.checked = state;
    const val = cb.dataset.val;
    if (group === 'zone') state ? activeZones.add(val) : activeZones.delete(val);
    else state ? activeVeh.add(val) : activeVeh.delete(val);
  }});
  applyFilters();
}}

document.getElementById('tot-count').textContent = allMarkers.length;
applyFilters();
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    master_path = config.CUSTOMER_MASTER
    output_path = "customer_map.html"

    print("Reading customer master...")
    df = load_customers(master_path)
    print(f"  {len(df)} customers loaded.")

    geojson  = build_geojson(df)
    polygons = build_polygons(df)
    all_zones = sorted(df["zone"].unique().tolist())

    print(f"  {len(all_zones)} zones found.")
    render_html(geojson, polygons, all_zones, output_path)

    print(f"\nMap saved: {output_path}")
    print(f"Open it in any browser.")


if __name__ == "__main__":
    main()
