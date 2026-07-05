"""
fleet_registry.py
-----------------
Zone Builder logic: reads historical delivery data + fleet definition,
clusters customers into workload-balanced zones, returns:
  - updated customer master DataFrame with zone column filled
  - zone summary for validation display
  - interactive Leaflet map HTML
"""

import json
import warnings
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans

warnings.filterwarnings("ignore")

# Special zones defined in config are always excluded from clustering
try:
    import config
    SPECIAL_ZONES = config.SPECIAL_ZONES
    DEPOT_LAT     = config.DEPOT_LAT
    DEPOT_LON     = config.DEPOT_LON
except Exception:
    SPECIAL_ZONES = {}
    DEPOT_LAT     = 42.005
    DEPOT_LON     = 21.435

# Total days in the historical dataset window (used for expected daily frequency)
_DATASET_DAYS = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2):
    from math import radians, cos, sin, asin, sqrt
    R = 6_371.0
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(max(0.0, a)))


def point_in_poly(lat, lon, polygon):
    n = len(polygon)
    inside = False
    x, y = lon, lat
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][1], polygon[i][0]
        xj, yj = polygon[j][1], polygon[j][0]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def detect_special_zone(lat, lon):
    for name, zdef in SPECIAL_ZONES.items():
        if point_in_poly(lat, lon, zdef["polygon"]):
            return name
    return None


# ── Core zone builder ──────────────────────────────────────────────────────────

def build_zones(history_df: pd.DataFrame, fleet_df: pd.DataFrame,
                master_df: pd.DataFrame | None = None):
    """
    Parameters
    ----------
    history_df  : historical deliveries (Delivery Date, Customer Code,
                  Customer Name, Latitude, Longitude, Total Weight, Vehicle Type)
    fleet_df    : fleet registry (vehicle_name, vehicle_type, capacity_kg,
                  max_trips_per_day)
    master_df   : existing customer master (optional); if provided, non-zone
                  columns are preserved

    Returns
    -------
    updated_master : pd.DataFrame with zone column assigned
    zone_summary   : list of dicts for validation table
    map_html       : str HTML of interactive Leaflet map
    quality        : dict of zone quality metrics (composite score 0-100)
    """
    global _DATASET_DAYS

    # ── 1. Clean history ───────────────────────────────────────────────────────
    df = history_df.copy()
    df.columns = df.columns.str.strip()
    for col in ["Latitude", "Longitude", "Total Weight"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Latitude", "Longitude", "Customer Code"])
    df = df[df.get("Total Weight", pd.Series(0, index=df.index)) <= 50_000]

    if "Delivery Date" in df.columns:
        df["Delivery Date"] = pd.to_datetime(df["Delivery Date"], errors="coerce")
        n_days = max(1, (df["Delivery Date"].max() - df["Delivery Date"].min()).days)
    else:
        n_days = 365
    _DATASET_DAYS = n_days

    # ── 2. Per-customer aggregation ────────────────────────────────────────────
    def agg_customer(grp):
        vt_counts  = grp["Vehicle Type"].value_counts() if "Vehicle Type" in grp.columns else pd.Series()
        dom_type   = vt_counts.index[0] if len(vt_counts) else "Kamion"
        avg_wt     = grp["Total Weight"].median() if "Total Weight" in grp.columns else 0
        name       = grp["Customer Name"].mode().iloc[0] if "Customer Name" in grp.columns else ""
        street     = grp["Street"].mode().iloc[0] if "Street" in grp.columns else ""
        visits     = len(grp)
        # Expected daily stops: how often this customer orders on average per day
        daily_freq = visits / n_days
        return pd.Series({
            "customer_name": name,
            "street":        street,
            "latitude":      grp["Latitude"].median(),
            "longitude":     grp["Longitude"].median(),
            "visits":        visits,
            "daily_freq":    daily_freq,
            "avg_weight_kg": round(float(avg_wt), 1),
            "dom_type":      dom_type,
        })

    cust = df.groupby("Customer Code").apply(
        agg_customer, include_groups=False
    ).reset_index()
    cust.rename(columns={"Customer Code": "customer_code"}, inplace=True)

    # ── 3. Special zone detection ──────────────────────────────────────────────
    cust["special_zone"] = cust.apply(
        lambda r: detect_special_zone(r["latitude"], r["longitude"]), axis=1
    )

    # ── 4. Fleet processing ────────────────────────────────────────────────────
    fl = fleet_df.copy()
    fl.columns = fl.columns.str.strip().str.lower()
    fl["vehicle_name"]      = fl["vehicle_name"].astype(str).str.strip()
    fl["vehicle_type"]      = fl["vehicle_type"].astype(str).str.strip()
    fl["capacity_kg"]       = pd.to_numeric(fl["capacity_kg"],       errors="coerce").fillna(3_200)
    fl["max_trips_per_day"] = pd.to_numeric(fl["max_trips_per_day"], errors="coerce").fillna(2).astype(int)
    fl["daily_cap_kg"]      = fl["capacity_kg"] * fl["max_trips_per_day"]

    # Exclude special-zone vehicles (Carsija = Van, Plostad = Kamion primary)
    special_veh_names = set()
    for sz_name, sz_def in SPECIAL_ZONES.items():
        # First vehicle of matching type goes to special zone
        ptype = sz_def.get("primary_vehicle", "Van")
        match = fl[fl["vehicle_type"] == ptype].head(1)
        if not match.empty:
            vname = match.iloc[0]["vehicle_name"]
            special_veh_names.add(vname)

    fl_regular = fl[~fl["vehicle_name"].isin(special_veh_names)].copy()

    # Count vehicles per type for clustering
    type_counts = fl_regular.groupby("vehicle_type")["vehicle_name"].apply(list).to_dict()

    # ── 5. Six-improvement clustering algorithm ────────────────────────────────
    #
    # #1  Data-driven anchor threshold (25th percentile of visits)
    # #2  Vardar river barrier as a feature dimension
    # #3  KMeans(weighted anchors) or AgglomerativeClustering(Ward) hybrid
    # #4  Separate stop-count AND weight objectives
    # #5  Simulated annealing Stage 3 with geographic constraint
    # #6  Zone quality score returned alongside summary

    from sklearn.cluster import AgglomerativeClustering

    cust["workload_score"] = cust["daily_freq"] * cust["avg_weight_kg"]
    cust["zone"] = None

    # Improvement 1: data-driven anchor threshold
    ANCHOR_THRESHOLD  = max(3, int(np.percentile(cust["visits"].values, 25)))
    BALANCE_TOLERANCE = 0.30

    # Improvement 2: Vardar river barrier
    RIVER_LAT      = 42.002
    RIVER_LON_WEST = 21.37
    RIVER_LON_EAST = 21.52
    RIVER_WEIGHT   = 0.12   # ~13 km equivalent; makes cross-river clustering costly

    def _river_side(lat, lon):
        if RIVER_LON_WEST <= lon <= RIVER_LON_EAST:
            return 1.0 if lat >= RIVER_LAT else 0.0
        return 0.5

    def _make_features(df):
        rs = df.apply(lambda r: _river_side(r["latitude"], r["longitude"]), axis=1).values
        return np.column_stack([df[["latitude","longitude"]].values, rs * RIVER_WEIGHT])

    # Improvements 4 + 5: SA balancing stop CV and weight CV with geographic guard
    def _sa_balance(sub_df, zone_labels_init, vehicle_names,
                    n_iter=5000, T_start=30.0, cooling=0.9985):
        import math
        labels   = np.array(zone_labels_init, dtype=object)
        n        = len(labels)
        lats_a   = sub_df["latitude"].values
        lons_a   = sub_df["longitude"].values
        ds       = sub_df["daily_freq"].values.astype(float)
        dw       = (sub_df["daily_freq"] * sub_df["avg_weight_kg"]).values.astype(float)
        vn_arr   = np.array(vehicle_names)

        zs   = {vn: 0.0 for vn in vehicle_names}
        zw   = {vn: 0.0 for vn in vehicle_names}
        zlat = {vn: 0.0 for vn in vehicle_names}
        zlon = {vn: 0.0 for vn in vehicle_names}
        zcnt = {vn: 0   for vn in vehicle_names}
        for i, vn in enumerate(labels):
            zs[vn]+=ds[i]; zw[vn]+=dw[i]
            zlat[vn]+=lats_a[i]; zlon[vn]+=lons_a[i]; zcnt[vn]+=1

        def _E():
            sv = np.array([zs[vn] for vn in vehicle_names])
            wv = np.array([zw[vn] for vn in vehicle_names])
            return 0.5*(sv.std()/max(sv.mean(),1e-9)) + 0.5*(wv.std()/max(wv.mean(),1e-9))

        T = T_start; E = _E(); best_labels = labels.copy(); best_E = E
        for _ in range(n_iter):
            i     = np.random.randint(n)
            old_z = labels[i]
            new_z = vn_arr[np.random.randint(len(vehicle_names))]
            if new_z == old_z: T*=cooling; continue
            nc = zcnt[new_z]
            if nc > 0:
                d_new = haversine_km(lats_a[i],lons_a[i], zlat[new_z]/nc, zlon[new_z]/nc)
                oc = zcnt[old_z]
                d_old = haversine_km(lats_a[i],lons_a[i], zlat[old_z]/oc, zlon[old_z]/oc) if oc>0 else 0
                if d_new > max(d_old*2.5, 8.0): T*=cooling; continue
            zs[old_z]-=ds[i]; zs[new_z]+=ds[i]
            zw[old_z]-=dw[i]; zw[new_z]+=dw[i]
            zlat[old_z]-=lats_a[i]; zlat[new_z]+=lats_a[i]
            zlon[old_z]-=lons_a[i]; zlon[new_z]+=lons_a[i]
            zcnt[old_z]-=1; zcnt[new_z]+=1
            labels[i] = new_z
            new_E = _E(); dE = new_E - E
            if dE < 0 or (T>0.01 and np.random.random() < math.exp(-dE/T)):
                E = new_E
                if E < best_E: best_E=E; best_labels=labels.copy()
            else:
                zs[new_z]-=ds[i]; zs[old_z]+=ds[i]
                zw[new_z]-=dw[i]; zw[old_z]+=dw[i]
                zlat[new_z]-=lats_a[i]; zlat[old_z]+=lats_a[i]
                zlon[new_z]-=lons_a[i]; zlon[old_z]+=lons_a[i]
                zcnt[new_z]-=1; zcnt[old_z]+=1; labels[i]=old_z
            T *= cooling
        return best_labels

    # Improvement 3: hybrid clustering
    def cluster_type_v2(sub_df, vehicle_names):
        if len(sub_df) == 0 or not vehicle_names:
            return sub_df

        n_clust = len(vehicle_names)
        sub_df  = sub_df.reset_index(drop=True)

        is_anchor = sub_df["visits"] >= ANCHOR_THRESHOLD
        n_anchors = int(is_anchor.sum())

        if n_anchors >= n_clust:
            # Fit KMeans on anchor customers (weighted by visits) + predict all
            anchor_feats = _make_features(sub_df[is_anchor])
            sw = sub_df.loc[is_anchor, "visits"].values.astype(float)
            sw = sw / sw.max()
            km = KMeans(n_clusters=n_clust, random_state=42, n_init=30, max_iter=500)
            km.fit(anchor_feats, sample_weight=sw)
            all_cluster_labels = km.predict(_make_features(sub_df))
        else:
            # AgglomerativeClustering(Ward) on all customers
            ag = AgglomerativeClustering(n_clusters=n_clust, linkage="ward")
            all_cluster_labels = ag.fit_predict(_make_features(sub_df))

        # Match cluster indices → vehicle names by workload capacity
        cluster_workload = {
            k: float(sub_df.loc[all_cluster_labels==k, "workload_score"].sum())
            for k in range(n_clust)
        }
        sorted_clusters = sorted(cluster_workload, key=lambda k: -cluster_workload[k])
        sorted_vehicles = sorted(
            vehicle_names,
            key=lambda vn: -fl_regular[fl_regular["vehicle_name"]==vn]["daily_cap_kg"].values[0]
            if vn in fl_regular["vehicle_name"].values else 0
        )
        cluster_to_zone = {ci: vn for ci, vn in zip(sorted_clusters, sorted_vehicles)}
        zone_labels = np.array([cluster_to_zone.get(l, sorted_vehicles[l % n_clust])
                                 for l in all_cluster_labels])

        # SA: balance stop CV + weight CV simultaneously
        zone_labels = _sa_balance(sub_df, zone_labels, vehicle_names)
        sub_df["zone"] = zone_labels
        return sub_df

    # Cluster each vehicle type separately
    parts = []
    for vtype, vnames in type_counts.items():
        mask = (cust["dom_type"] == vtype) & (cust["special_zone"].isna())
        sub  = cust[mask].copy()
        if len(sub) > 0:
            parts.append(cluster_type_v2(sub, vnames))

    # Customers with no matching vehicle type → assign to nearest cluster centre
    assigned_codes = set()
    for p in parts:
        assigned_codes.update(p["customer_code"].tolist())

    unmatched = cust[~cust["customer_code"].isin(assigned_codes) &
                     cust["special_zone"].isna()].copy()
    if len(unmatched) > 0:
        # Build all zone centres from already-clustered parts
        all_centres = []
        for p in parts:
            for z in p["zone"].dropna().unique():
                zm = p[p["zone"] == z]
                all_centres.append({
                    "zone": z,
                    "lat":  zm["latitude"].mean(),
                    "lon":  zm["longitude"].mean(),
                })
        if all_centres:
            for i, row in unmatched.iterrows():
                nearest = min(all_centres,
                              key=lambda c: haversine_km(row["latitude"], row["longitude"],
                                                         c["lat"], c["lon"]))
                unmatched.at[i, "zone"] = nearest["zone"]
        else:
            all_vnames = [v for vnames in type_counts.values() for v in vnames]
            if all_vnames:
                unmatched["zone"] = all_vnames[0]
        parts.append(unmatched)

    # Special zone customers
    spec_part = cust[cust["special_zone"].notna()].copy()
    for _, row in spec_part.iterrows():
        sz    = row["special_zone"]
        ptype = SPECIAL_ZONES[sz].get("primary_vehicle", "Van") if sz in SPECIAL_ZONES else "Van"
        spec_veh  = fl[fl["vehicle_type"] == ptype].head(1)
        zone_name = spec_veh.iloc[0]["vehicle_name"] if not spec_veh.empty else sz
        spec_part.loc[spec_part["customer_code"] == row["customer_code"], "zone"] = zone_name

    if len(spec_part) > 0:
        parts.append(spec_part)

    cust_zoned = pd.concat(parts, ignore_index=True) if parts else cust.copy()

    # ── 6. Build zone summary ──────────────────────────────────────────────────
    zone_summary = []
    for zone_name in sorted(cust_zoned["zone"].dropna().unique()):
        zdf    = cust_zoned[cust_zoned["zone"] == zone_name]
        n_cust = len(zdf)
        exp_daily_stops  = zdf["daily_freq"].sum()          # expected stops/day
        exp_daily_weight = (zdf["daily_freq"] * zdf["avg_weight_kg"]).sum()

        # Find vehicle capacity
        veh_row = fl[fl["vehicle_name"] == zone_name]
        if not veh_row.empty:
            trip_cap  = veh_row.iloc[0]["capacity_kg"]
            daily_cap = veh_row.iloc[0]["daily_cap_kg"]
            vtype     = veh_row.iloc[0]["vehicle_type"]
        else:
            trip_cap  = 0
            daily_cap = 0
            vtype     = "?"

        # Flag if expected daily weight exceeds daily capacity
        utilisation = exp_daily_weight / daily_cap * 100 if daily_cap > 0 else 0
        if utilisation > 90:
            flag = "⚠️ OVERLOADED — consider splitting"
        elif utilisation > 70:
            flag = "🟡 Heavy"
        elif exp_daily_stops < 1:
            flag = "💤 Very light — consider merging"
        else:
            flag = "✅ OK"

        zone_summary.append({
            "zone":              zone_name,
            "vehicle_type":      vtype,
            "customers":         n_cust,
            "exp_daily_stops":   round(exp_daily_stops, 1),
            "exp_daily_kg":      round(exp_daily_weight, 0),
            "trip_capacity_kg":  trip_cap,
            "daily_capacity_kg": daily_cap,
            "utilisation_pct":   round(utilisation, 1),
            "flag":              flag,
        })

    # ── 7. Build updated customer master ──────────────────────────────────────
    # Map customer_code → zone
    zone_map = dict(zip(cust_zoned["customer_code"].astype(str),
                        cust_zoned["zone"]))

    if master_df is not None:
        updated = master_df.copy()
        updated.columns = updated.columns.str.strip().str.lower()
        updated["customer_code"] = updated["customer_code"].astype(str).str.strip()
        updated["zone"] = updated["customer_code"].map(zone_map).fillna(updated.get("zone", ""))
    else:
        # Build from scratch
        updated = cust_zoned.rename(columns={"dom_type": "preferred_vehicle"})[
            ["customer_code", "customer_name", "street",
             "latitude", "longitude", "zone",
             "visits", "avg_weight_kg", "preferred_vehicle"]
        ].copy()
        updated["special_zone"]       = cust_zoned["special_zone"].fillna("")
        updated["eligible_vehicles"]  = updated["preferred_vehicle"]
        updated["time_window_start"]  = "06:00"
        updated["time_window_end"]    = "18:00"
        updated["avg_cases"]          = 0
        updated["kg_per_case"]        = 0
        updated["vehicle_breakdown"]  = ""
        updated["notes"]              = ""

    # ── 8. Build interactive map ───────────────────────────────────────────────
    map_html = _build_zone_map(cust_zoned, zone_summary, fl)

    # ── Improvement 6: Zone quality score ─────────────────────────────────────
    stops_arr  = np.array([z["exp_daily_stops"] for z in zone_summary])
    weight_arr = np.array([z["exp_daily_kg"]    for z in zone_summary])
    stop_cv    = stops_arr.std()  / max(stops_arr.mean(),  1e-9)
    weight_cv  = weight_arr.std() / max(weight_arr.mean(), 1e-9)

    # Average intra-zone distance from centre
    compactness_vals = []
    for z in zone_summary:
        zdf = cust_zoned[cust_zoned["zone"] == z["zone"]]
        if len(zdf) <= 1:
            compactness_vals.append(0.0)
            continue
        clat, clon = zdf["latitude"].mean(), zdf["longitude"].mean()
        dists = zdf.apply(
            lambda r: haversine_km(r["latitude"], r["longitude"], clat, clon), axis=1
        )
        compactness_vals.append(float(dists.mean()))
    avg_compact_km = float(np.mean(compactness_vals)) if compactness_vals else 0.0

    stop_score    = round(max(0.0, 100.0 * (1.0 - stop_cv)),    1)
    weight_score  = round(max(0.0, 100.0 * (1.0 - weight_cv)),  1)
    compact_score = round(max(0.0, 100.0 * (1.0 - avg_compact_km / 5.0)), 1)
    composite     = round(0.40 * stop_score + 0.30 * weight_score + 0.30 * compact_score, 1)

    quality = {
        "stop_balance_cv":    round(stop_cv,         3),
        "weight_balance_cv":  round(weight_cv,        3),
        "avg_compactness_km": round(avg_compact_km,   2),
        "stop_score":         stop_score,
        "weight_score":       weight_score,
        "compactness_score":  compact_score,
        "composite_score":    composite,
        "anchor_threshold":   ANCHOR_THRESHOLD,
    }

    return updated, zone_summary, map_html, quality


# ── Map builder ────────────────────────────────────────────────────────────────

_PALETTE = [
    "#1A5276","#1E8449","#D35400","#8E44AD","#C0392B","#17A589",
    "#B7950B","#2980B9","#27AE60","#E67E22","#6C3483","#E74C3C",
    "#1ABC9C","#F39C12","#5D6D7E","#A93226","#117A65","#CA6F1E",
    "#7D3C98","#154360","#0E6655","#BA4A00","#1F618D","#52BE80",
]


def _build_zone_map(cust_df: pd.DataFrame, zone_summary: list,
                    fleet_df: pd.DataFrame) -> str:
    """Build a self-contained Leaflet HTML map showing zones + customers."""

    # Assign colours per zone
    all_zones   = sorted(cust_df["zone"].dropna().unique())
    zone_colour = {z: _PALETTE[i % len(_PALETTE)] for i, z in enumerate(all_zones)}

    all_types = sorted(cust_df["dom_type"].unique())

    # GeoJSON features
    features = []
    for _, row in cust_df.iterrows():
        zone = str(row.get("zone", "")) or "Unzoned"
        clr  = zone_colour.get(zone, "#7F8C8D")
        util = next((z["utilisation_pct"] for z in zone_summary
                     if z["zone"] == zone), 0)
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [row["longitude"], row["latitude"]],
            },
            "properties": {
                "zone":    zone,
                "vtype":   str(row.get("dom_type", "")),
                "name":    str(row.get("customer_name", "")),
                "code":    str(row.get("customer_code", "")),
                "visits":  int(row.get("visits", 0)),
                "avg_kg":  round(float(row.get("avg_weight_kg", 0)), 0),
                "colour":  clr,
                "util":    util,
            },
        })

    geojson_js   = json.dumps({"type": "FeatureCollection", "features": features},
                              ensure_ascii=False)
    zones_js     = json.dumps(all_zones, ensure_ascii=False)
    types_js     = json.dumps(all_types, ensure_ascii=False)
    colours_js   = json.dumps(zone_colour, ensure_ascii=False)

    # Zone summary table rows
    summary_rows = ""
    for z in zone_summary:
        flag_class = ("red" if "OVER" in z["flag"]
                      else "yellow" if "Heavy" in z["flag"]
                      else "blue" if "light" in z["flag"]
                      else "green")
        summary_rows += (
            f"<tr class='{flag_class}'>"
            f"<td>{z['zone']}</td>"
            f"<td>{z['vehicle_type']}</td>"
            f"<td>{z['customers']}</td>"
            f"<td>{z['exp_daily_stops']}</td>"
            f"<td>{z['exp_daily_kg']:,.0f}</td>"
            f"<td>{z['trip_capacity_kg']:,}</td>"
            f"<td>{z['utilisation_pct']}%</td>"
            f"<td>{z['flag']}</td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>DSD Zone Builder Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',sans-serif;display:flex;flex-direction:column;height:100vh}}
  #top{{display:flex;flex:1;overflow:hidden}}
  #sidebar{{width:270px;background:#1F2D3D;color:#ECF0F1;display:flex;flex-direction:column;overflow:hidden}}
  #sidebar h1{{font-size:13px;font-weight:700;padding:10px 12px 6px;border-bottom:1px solid #2C3E50;color:#fff}}
  #sidebar h2{{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:#95A5A6;padding:7px 12px 3px}}
  #fw{{flex:1;overflow-y:auto;padding-bottom:8px}}
  .fs{{padding:0 12px}}
  .cr{{display:flex;align-items:center;gap:6px;padding:2px 0;font-size:11px;cursor:pointer;user-select:none}}
  .cr input{{cursor:pointer;accent-color:#3498DB;width:12px;height:12px}}
  .sw{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
  .br{{display:flex;gap:4px;padding:4px 12px}}
  .btn{{flex:1;padding:3px 0;font-size:10px;font-weight:600;border:none;border-radius:4px;cursor:pointer;background:#2C3E50;color:#BDC3C7}}
  .btn:hover{{background:#3498DB;color:#fff}}
  #stats{{padding:5px 12px;font-size:10px;color:#7F8C8D;border-top:1px solid #2C3E50}}
  #map{{flex:1}}
  #bottom{{height:240px;overflow-y:auto;border-top:2px solid #2C3E50;background:#fff}}
  table{{width:100%;border-collapse:collapse;font-size:11px}}
  th{{background:#1F4E79;color:#fff;padding:5px 8px;text-align:left;position:sticky;top:0}}
  td{{padding:4px 8px;border-bottom:1px solid #eee}}
  tr.green td{{background:#d4f5d4}}
  tr.yellow td{{background:#fff9c4}}
  tr.red td{{background:#fdd}}
  tr.blue td{{background:#d4eaf7}}
  .lf-popup{{font-size:11px;line-height:1.5;min-width:180px}}
</style>
</head>
<body>
<div id="top">
<div id="sidebar">
  <h1>🗺 Zone Builder Map</h1>
  <div id="fw">
    <h2>Vehicle Type</h2>
    <div class="fs" id="type-f"></div>
    <div class="br">
      <button class="btn" onclick="tA('type',true)">All</button>
      <button class="btn" onclick="tA('type',false)">None</button>
    </div>
    <h2>Zone</h2>
    <div class="fs" id="zone-f"></div>
    <div class="br">
      <button class="btn" onclick="tA('zone',true)">All</button>
      <button class="btn" onclick="tA('zone',false)">None</button>
    </div>
  </div>
  <div id="stats">Showing <b id="vc">-</b> of <b id="tc">-</b> customers</div>
</div>
<div id="map"></div>
</div>
<div id="bottom">
  <table>
    <tr>
      <th>Zone</th><th>Type</th><th>Customers</th>
      <th>Exp. Stops/Day</th><th>Exp. KG/Day</th>
      <th>Trip Cap (kg)</th><th>Utilisation</th><th>Status</th>
    </tr>
    {summary_rows}
  </table>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const GJ     = {geojson_js};
const ZONES  = {zones_js};
const TYPES  = {types_js};
const ZCLR   = {colours_js};

const map = L.map('map').setView([41.998,21.435],12);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
  {{attribution:'© OpenStreetMap',maxZoom:19}}).addTo(map);

const markers=[];
GJ.features.forEach(f=>{{
  const p=f.properties;
  const mk=L.circleMarker([f.geometry.coordinates[1],f.geometry.coordinates[0]],{{
    radius:5,fillColor:p.colour,color:'#fff',weight:1,fillOpacity:0.85
  }});
  mk.bindPopup(`<div class="lf-popup">
    <b>${{p.name}}</b><br>
    <span style="color:#7F8C8D;font-size:10px">${{p.code}}</span><br>
    Zone: <b style="color:${{p.colour}}">${{p.zone}}</b><br>
    Type: ${{p.vtype}} | Visits: ${{p.visits}} | Avg: ${{p.avg_kg}} kg
  </div>`);
  mk._z=p.zone; mk._t=p.vtype;
  markers.push(mk);
}});
const mg=L.layerGroup(markers).addTo(map);
const aZ=new Set(ZONES), aT=new Set(TYPES);
function apply(){{
  let v=0;
  markers.forEach(m=>{{
    const ok=aZ.has(m._z)&&aT.has(m._t);
    if(ok){{if(!map.hasLayer(m))mg.addLayer(m);v++;}}
    else{{if(map.hasLayer(m))mg.removeLayer(m);}}
  }});
  document.getElementById('vc').textContent=v;
}}
function mk2(wrap,label,val,clr,group,set){{
  const lb=document.createElement('label');lb.className='cr';
  lb.innerHTML=`<input type="checkbox" data-g="${{group}}" data-v="${{val}}" checked>
    <span class="sw" style="background:${{clr}}"></span><span>${{label}}</span>`;
  lb.querySelector('input').addEventListener('change',e=>{{
    e.target.checked?set.add(val):set.delete(val);apply();
  }});
  document.getElementById(wrap).appendChild(lb);
}}
const TCLR={{Kamion:'#2E86C1',Furgon:'#1E8449',Van:'#D35400'}};
TYPES.forEach(t=>mk2('type-f',t,t,TCLR[t]||'#7F8C8D','type',aT));
ZONES.forEach(z=>mk2('zone-f',z,z,ZCLR[z]||'#7F8C8D','zone',aZ));
function tA(g,s){{
  document.querySelectorAll(`input[data-g="${{g}}"]`).forEach(cb=>{{
    cb.checked=s;const v=cb.dataset.v;
    if(g==='type')s?aT.add(v):aT.delete(v);
    else s?aZ.add(v):aZ.delete(v);
  }});apply();
}}
document.getElementById('tc').textContent=markers.length;
apply();
</script>
</body>
</html>"""

    return html
