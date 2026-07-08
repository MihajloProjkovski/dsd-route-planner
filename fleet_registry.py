"""
fleet_registry.py  v2.1
-----------------
Zone Builder logic: reads historical delivery data + fleet definition,
clusters customers into workload-balanced zones, returns:
  - updated customer master DataFrame with zone column filled
  - zone summary for validation display
  - interactive Leaflet map HTML
  - quality metrics dict

Two modes:
  "fleet"  — zones = number of vehicles (current behaviour)
  "auto"   — silhouette-optimal k per type; recommends fleet assignment
"""

import json
import warnings
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans, DBSCAN
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

# Special zones defined in config are always excluded from clustering
try:
    import config
    SPECIAL_ZONES          = config.SPECIAL_ZONES
    DEPOT_LAT              = config.DEPOT_LAT
    DEPOT_LON              = config.DEPOT_LON
    MAX_STOPS_PER_DAY      = config.MAX_STOPS_PER_DAY
    MAX_CUSTOMERS_PER_ZONE = config.MAX_CUSTOMERS_PER_ZONE
    MAX_ZONE_RADIUS_KM     = config.MAX_ZONE_RADIUS_KM
    URBAN_ZONE_RADIUS_KM   = config.URBAN_ZONE_RADIUS_KM
    RURAL_ZONE_RADIUS_KM   = config.RURAL_ZONE_RADIUS_KM
    URBAN_CORE_RADIUS_KM   = config.URBAN_CORE_RADIUS_KM
    ZONE_CROSS_TYPE_ELIGIBLE_PCT = config.ZONE_CROSS_TYPE_ELIGIBLE_PCT
except Exception:
    SPECIAL_ZONES          = {}
    DEPOT_LAT              = 42.005
    DEPOT_LON              = 21.435
    MAX_STOPS_PER_DAY      = 12
    MAX_CUSTOMERS_PER_ZONE = 60
    MAX_ZONE_RADIUS_KM     = 3.5
    URBAN_ZONE_RADIUS_KM   = 1.5
    RURAL_ZONE_RADIUS_KM   = 2.0
    URBAN_CORE_RADIUS_KM   = 6.0
    ZONE_CROSS_TYPE_ELIGIBLE_PCT = 70

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


def _cluster_stats_km(coords: np.ndarray, labels: np.ndarray) -> dict:
    """Per-cluster (centroid_lat, centroid_lon, avg_radius_km), measured on
    raw lat/lon (mirrors the zone quality-score compactness calc)."""
    df = pd.DataFrame(coords, columns=["latitude", "longitude"])
    df["label"] = labels
    out = {}
    for label, grp in df.groupby("label"):
        clat, clon = grp["latitude"].mean(), grp["longitude"].mean()
        d = grp.apply(lambda r: haversine_km(r["latitude"], r["longitude"], clat, clon), axis=1)
        out[label] = (clat, clon, float(d.mean()))
    return out


def _zone_radius_target_km(clat: float, clon: float) -> float:
    """Distance-from-depot-adaptive per-zone radius target: tighter inside
    the urban core, looser outside it."""
    dist_from_depot = haversine_km(clat, clon, DEPOT_LAT, DEPOT_LON)
    return URBAN_ZONE_RADIUS_KM if dist_from_depot <= URBAN_CORE_RADIUS_KM else RURAL_ZONE_RADIUS_KM


# ── Core zone builder ──────────────────────────────────────────────────────────

def find_optimal_zones(coords: np.ndarray, min_zones: int = 2,
                       max_zones: int = 30, radius_aware: bool = False,
                       return_diagnostics: bool = False):
    """
    Find the natural number of geographic zones using silhouette score
    optimization over k-means (more stable than DBSCAN for variable densities).
    If radius_aware, prefers the best-silhouette k among candidates where
    EVERY individual zone's avg radius is within its own urban/rural target
    (see _zone_radius_target_km); falls back to the k with the smallest worst
    zone if none qualify. If return_diagnostics, returns (k, achieved_radius_km)
    — the worst zone's radius for the chosen k — instead of just k.
    """
    if len(coords) < max(min_zones * 3, 10):
        k = max(min_zones, len(coords) // 5)
        return (k, None) if return_diagnostics else k

    scaler  = StandardScaler()
    X       = scaler.fit_transform(coords)
    scores  = {}
    cluster_stats = {}
    for k in range(min_zones, min(max_zones + 1, len(coords) // 3)):
        km  = KMeans(n_clusters=k, random_state=42, n_init=10, max_iter=300)
        lbl = km.fit_predict(X)
        if len(set(lbl)) < 2:
            continue
        try:
            scores[k] = silhouette_score(X, lbl, sample_size=min(500, len(X)))
        except Exception:
            continue
        cluster_stats[k] = _cluster_stats_km(coords, lbl)

    if not scores:
        return (min_zones, None) if return_diagnostics else min_zones

    if not radius_aware:
        best_k, achieved = max(scores, key=scores.get), None
    else:
        def worst_excess(k):
            return max((r - _zone_radius_target_km(lat, lon)
                        for lat, lon, r in cluster_stats[k].values()), default=0.0)

        qualifying = {k: s for k, s in scores.items() if worst_excess(k) <= 0}
        best_k = max(qualifying, key=qualifying.get) if qualifying else min(scores, key=worst_excess)
        achieved = max((r for _, _, r in cluster_stats[best_k].values()), default=0.0)

    return (best_k, achieved) if return_diagnostics else best_k


def build_zones(history_df: pd.DataFrame, fleet_df: pd.DataFrame,
                master_df: pd.DataFrame | None = None,
                mode: str = "fleet"):
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
    updated_master       : pd.DataFrame with zone column assigned
    zone_summary         : list of dicts for validation table
    map_html             : str HTML of interactive Leaflet map
    quality              : dict of zone quality metrics (composite score 0-100)
    fleet_recommendation : dict with natural zone counts per type (auto mode only)
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
        total_hist = len(grp)
        avg_wt     = grp["Total Weight"].median() if "Total Weight" in grp.columns else 0
        name       = grp["Customer Name"].mode().iloc[0] if "Customer Name" in grp.columns else ""
        street     = grp["Street"].mode().iloc[0] if "Street" in grp.columns else ""

        # ── Three-rule vehicle eligibility ──────────────────────────────────
        # Mirrors the classification in build_customer_master.py:
        # Furgon-dominant → Furgon,Kamion,Van
        # Kamion-dominant → Kamion,Van  (+Furgon if Furgon% ≥ 10%)
        # Van-dominant    → Van only    (+Kamion if Kamion% ≥ 15%, +Furgon if ≥ 15%)
        furgon_pct = vt_counts.get("Furgon", 0) / max(total_hist, 1) * 100
        kamion_pct = vt_counts.get("Kamion", 0) / max(total_hist, 1) * 100
        if dom_type == "Furgon":
            eligible = ["Furgon", "Kamion", "Van"]
        elif dom_type == "Kamion":
            eligible = ["Kamion", "Van"]
            if furgon_pct >= 10:
                eligible.append("Furgon")
        else:   # Van-dominant
            eligible = ["Van"]
            if kamion_pct >= 15:
                eligible.append("Kamion")
            if furgon_pct >= 15:
                eligible.append("Furgon")
        eligible_str = ",".join(sorted(eligible))

        # Vehicle breakdown string: "Kamion:88.5%  Van:10.4%"
        breakdown = "  ".join(
            f"{k}:{v/total_hist*100:.1f}%"
            for k, v in sorted(vt_counts.items(), key=lambda x: -x[1])
        ) if len(vt_counts) else ""

        # Improvement #1: distinct delivery days
        visits     = len(grp)
        if "Delivery Date" in grp.columns:
            distinct_days = grp["Delivery Date"].nunique()
        else:
            distinct_days = visits

        daily_freq = distinct_days / n_days

        if "Delivery Date" in grp.columns and distinct_days >= 4:
            all_dates    = pd.date_range(grp["Delivery Date"].min(),
                                         grp["Delivery Date"].max(), freq="D")
            order_days   = set(grp["Delivery Date"].dt.date)
            daily_series = np.array([1 if d.date() in order_days else 0
                                     for d in all_dates], dtype=float)
            p75_freq = float(np.percentile(daily_series, 75)) if len(daily_series) > 0 else daily_freq
        else:
            p75_freq = daily_freq * 0.75

        return pd.Series({
            "customer_name":     name,
            "street":            street,
            "latitude":          grp["Latitude"].median(),
            "longitude":         grp["Longitude"].median(),
            "visits":            distinct_days,
            "total_rows":        visits,
            "daily_freq":        daily_freq,
            "p75_freq":          p75_freq,
            "avg_weight_kg":     round(float(avg_wt), 1),
            "avg_cases":         round(float(grp["Number of Cases"].median()), 1)
                                 if "Number of Cases" in grp.columns else 0,
            "dom_type":          dom_type,
            "eligible_vehicles": eligible_str,
            "vehicle_breakdown": breakdown,
            "preferred_vehicle": dom_type,
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

    # ── Determine zone counts per vehicle type ────────────────────────────────
    # mode="fleet"  → zones = number of fleet vehicles (original behaviour)
    # mode="auto"   → silhouette-optimal k per type; vehicles not matched = Float
    if mode == "auto":
        auto_zone_counts = {}
        fleet_recommendation = {}
        mask_ns = cust["special_zone"].isna()
        for vtype in ["Kamion", "Furgon", "Van"]:
            type_mask  = mask_ns & (cust["dom_type"] == vtype)
            sub_coords = cust.loc[type_mask, ["latitude","longitude"]].values
            n_fleet    = len(fl_regular[fl_regular["vehicle_type"] == vtype])
            n_cust     = len(sub_coords)

            total_p75_stops    = float(cust.loc[type_mask, "p75_freq"].sum())
            workload_min_zones = max(1, int(np.ceil(total_p75_stops / MAX_STOPS_PER_DAY)))
            customer_min_zones = max(1, int(np.ceil(n_cust / MAX_CUSTOMERS_PER_ZONE)))
            floor_zones = max(2, workload_min_zones, customer_min_zones)
            # defensive: never propose more zones than ~2 customers/zone could support
            floor_zones = min(floor_zones, max(1, n_cust // 2)) if n_cust > 0 else floor_zones

            achieved_radius_km = None
            if n_cust < 6:
                auto_zone_counts[vtype] = 1
            else:
                # Zone count is sized purely by geography/workload/customer-count
                # targets — NOT capped by fleet size. Vehicle coverage of the
                # resulting zones is a separate concern, handled below.
                zone_upper_bound = min(n_cust // 3, 50)
                auto_zone_counts[vtype], achieved_radius_km = find_optimal_zones(
                    sub_coords,
                    min_zones=floor_zones,
                    max_zones=zone_upper_bound,
                    radius_aware=True,
                    return_diagnostics=True,
                )

            n_zones     = auto_zone_counts[vtype]
            dedicated   = min(n_zones, n_fleet)
            unclaimed   = max(0, n_zones - n_fleet)
            spare_float = max(0, n_fleet - n_zones)

            coverage_msg = (
                f"{n_zones} {vtype} zone(s) created from geography/workload/customer-count "
                f"targets. {dedicated} get a dedicated vehicle" +
                (f"; {unclaimed} zone(s) have no dedicated vehicle and will be served "
                 f"dynamically by Float vehicles." if unclaimed > 0 else ".") +
                (f" {spare_float} extra {vtype} vehicle(s) beyond the natural zone count "
                 f"are available as Float capacity." if spare_float > 0 else "")
            )

            fleet_recommendation[vtype] = {
                "n_zones":            n_zones,
                "fleet_available":    n_fleet,
                "dedicated_zones":    dedicated,
                "unclaimed_zones":    unclaimed,
                "spare_float":        spare_float,
                "workload_min_zones": workload_min_zones,
                "customer_min_zones": customer_min_zones,
                "achieved_radius_km": round(achieved_radius_km, 2) if achieved_radius_km is not None else None,
                "coverage_msg":       coverage_msg,
            }

        # Generic zone naming, decoupled from real vehicle identity
        type_counts      = {}
        zone_type_map    = {}
        zone_vehicle_map = {}
        for vtype, n_zones in auto_zone_counts.items():
            zone_names = [f"{vtype}_{i+1:02d}" for i in range(n_zones)]
            type_counts[vtype] = zone_names
            for zn in zone_names:
                zone_type_map[zn] = vtype
            # busiest-zone-first ("_01") <-> highest-capacity-vehicle-first pairing
            vehicles_sorted = (fl_regular[fl_regular["vehicle_type"] == vtype]
                               .sort_values("daily_cap_kg", ascending=False)["vehicle_name"].tolist())
            zone_vehicle_map.update(dict(zip(zone_names, vehicles_sorted)))  # caps at min(n_zones, n_fleet)

        # Store recommendation for return
        _auto_recommendation = fleet_recommendation
    else:
        # Fleet mode: zones = vehicles (original)
        type_counts = fl_regular.groupby("vehicle_type")["vehicle_name"].apply(list).to_dict()
        zone_type_map, zone_vehicle_map = {}, {}
        _auto_recommendation = None

    # ── 5. Six-improvement clustering algorithm ────────────────────────────────
    #
    # #1  Data-driven anchor threshold (25th percentile of visits)
    # #2  Vardar river barrier as a feature dimension
    # #3  KMeans(weighted anchors) or AgglomerativeClustering(Ward) hybrid
    # #4  Separate stop-count AND weight objectives
    # #5  Simulated annealing Stage 3 with geographic constraint
    # #6  Zone quality score returned alongside summary

    from sklearn.cluster import AgglomerativeClustering

    cust["workload_score"] = cust["p75_freq"] * cust["avg_weight_kg"]
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
    # Fix 1: tightened geographic guard — max 3 km farther than own zone centre
    # Fix 2: SA only considers K=3 nearest zones per customer (no cross-city moves)
    _SA_MAX_EXTRA_KM = 3.0   # a move is rejected if target is >3 km farther than own centre
    _SA_K_NEAREST    = 3     # each customer can only move to one of its K nearest zones

    def _sa_balance(sub_df, zone_labels_init, vehicle_names,
                    n_iter=5000, T_start=30.0, cooling=0.9985):
        import math
        labels   = np.array(zone_labels_init, dtype=object)
        n        = len(labels)
        lats_a   = sub_df["latitude"].values
        lons_a   = sub_df["longitude"].values
        ds       = sub_df["p75_freq"].values.astype(float)   # p75 demand for sizing
        dw       = (sub_df["daily_freq"] * sub_df["avg_weight_kg"]).values.astype(float)
        vn_arr   = np.array(vehicle_names)
        k_near   = min(_SA_K_NEAREST, len(vehicle_names))

        zs   = {vn: 0.0 for vn in vehicle_names}
        zw   = {vn: 0.0 for vn in vehicle_names}
        zlat = {vn: 0.0 for vn in vehicle_names}
        zlon = {vn: 0.0 for vn in vehicle_names}
        zcnt = {vn: 0   for vn in vehicle_names}
        for i, vn in enumerate(labels):
            zs[vn]+=ds[i]; zw[vn]+=dw[i]
            zlat[vn]+=lats_a[i]; zlon[vn]+=lons_a[i]; zcnt[vn]+=1

        def _zone_centre(vn):
            c = zcnt[vn]
            return (zlat[vn]/c, zlon[vn]/c) if c > 0 else (DEPOT_LAT, DEPOT_LON)

        def _nearest_zones(i):
            """Return K nearest vehicle names to customer i (excluding own zone)."""
            own_z = labels[i]
            dists = []
            for vn in vehicle_names:
                if vn == own_z: continue
                clat, clon = _zone_centre(vn)
                dists.append((haversine_km(lats_a[i], lons_a[i], clat, clon), vn))
            dists.sort(key=lambda x: x[0])
            return [vn for _, vn in dists[:k_near]]

        def _E():
            # Fix 1+3: compute CV per-type, average across types
            # This prevents inter-type imbalance from inflating the energy function.
            type_cvs_s = []
            type_cvs_w = []
            # group zones by vehicle type via fl_regular lookup
            vtype_map = {}
            for vn in vehicle_names:
                rows = fl_regular[fl_regular["vehicle_name"] == vn]
                vtype_map[vn] = rows.iloc[0]["vehicle_type"] if not rows.empty else "?"
            types_present = set(vtype_map.values())
            for vt in types_present:
                vns_t = [vn for vn, vt2 in vtype_map.items() if vt2 == vt]
                if len(vns_t) < 2: continue
                sv = np.array([zs[vn] for vn in vns_t])
                wv = np.array([zw[vn] for vn in vns_t])
                type_cvs_s.append(sv.std() / max(sv.mean(), 1e-9))
                type_cvs_w.append(wv.std() / max(wv.mean(), 1e-9))
            if not type_cvs_s:
                sv = np.array([zs[vn] for vn in vehicle_names])
                wv = np.array([zw[vn] for vn in vehicle_names])
                return 0.5*(sv.std()/max(sv.mean(),1e-9)) + 0.5*(wv.std()/max(wv.mean(),1e-9))
            return 0.5*float(np.mean(type_cvs_s)) + 0.5*float(np.mean(type_cvs_w))

        T = T_start; E = _E(); best_labels = labels.copy(); best_E = E
        for _ in range(n_iter):
            i     = np.random.randint(n)
            old_z = labels[i]

            # Fix 2: only pick from K nearest zones
            candidates = _nearest_zones(i)
            if not candidates: T*=cooling; continue
            new_z = candidates[np.random.randint(len(candidates))]

            # Fix 1: tightened geographic guard
            oc = zcnt[old_z]
            d_old = haversine_km(lats_a[i],lons_a[i], *_zone_centre(old_z)) if oc > 0 else 0
            d_new = haversine_km(lats_a[i],lons_a[i], *_zone_centre(new_z))
            if d_new > d_old + _SA_MAX_EXTRA_KM: T*=cooling; continue

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
        # Crash-prevention backstop: KMeans/AgglomerativeClustering require
        # n_clusters <= n_samples. Unreachable with today's config values but
        # zone count is no longer clamped to fleet size, so this is real insurance.
        n_clust = min(n_clust, len(sub_df))
        vehicle_names = vehicle_names[:n_clust]
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
        # Mean expected stops (average day)
        exp_daily_stops  = zdf["daily_freq"].sum()
        exp_daily_weight = (zdf["daily_freq"] * zdf["avg_weight_kg"]).sum()
        # P75 expected stops (busy day) — used for utilisation flag
        p75_daily_stops  = zdf["p75_freq"].sum()
        p75_daily_weight = (zdf["p75_freq"] * zdf["avg_weight_kg"]).sum()

        resolved_vname = zone_vehicle_map.get(zone_name, zone_name)   # no-op for fleet/special-zone paths
        veh_row = fl[fl["vehicle_name"] == resolved_vname]
        if not veh_row.empty:
            trip_cap  = veh_row.iloc[0]["capacity_kg"]
            daily_cap = veh_row.iloc[0]["daily_cap_kg"]
            vtype     = veh_row.iloc[0]["vehicle_type"]
        else:
            # No dedicated vehicle (auto-mode unclaimed zone) — fall back to
            # the type-level average so utilisation still means something.
            vtype = zone_type_map.get(zone_name, "?")
            type_fleet = fl_regular[fl_regular["vehicle_type"] == vtype]
            if len(type_fleet) > 0:
                trip_cap  = type_fleet["capacity_kg"].mean()
                daily_cap = type_fleet["daily_cap_kg"].mean()
            else:
                trip_cap, daily_cap = 0, 0

        # Utilisation based on p75 demand — flags zones that overflow on busy days
        utilisation = p75_daily_weight / daily_cap * 100 if daily_cap > 0 else 0
        if n_cust > MAX_CUSTOMERS_PER_ZONE:
            flag = f"🔴 OVER customer cap ({n_cust}/{MAX_CUSTOMERS_PER_ZONE}) — needs more {vtype} vehicles"
        elif utilisation > 90:
            flag = "⚠️ OVERLOADED on busy days — consider splitting"
        elif utilisation > 70:
            flag = "🟡 Heavy on busy days"
        elif exp_daily_stops < 1:
            flag = "💤 Very light — consider merging"
        else:
            flag = "✅ OK"

        zone_summary.append({
            "zone":              zone_name,
            "vehicle_type":      vtype,
            "centroid_lat":      round(float(zdf["latitude"].mean()), 6),
            "centroid_lon":      round(float(zdf["longitude"].mean()), 6),
            "customers":         n_cust,
            "exp_daily_stops":   round(exp_daily_stops, 1),
            "p75_daily_stops":   round(p75_daily_stops, 1),
            "exp_daily_kg":      round(exp_daily_weight, 0),
            "p75_daily_kg":      round(p75_daily_weight, 0),
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
        # Also update eligible_vehicles from fresh classification
        elig_map = dict(zip(cust_zoned["customer_code"].astype(str),
                            cust_zoned["eligible_vehicles"]))
        updated["eligible_vehicles"] = updated["customer_code"].map(elig_map).fillna(
            updated.get("eligible_vehicles", "Kamion,Van"))
        bd_map = dict(zip(cust_zoned["customer_code"].astype(str),
                          cust_zoned["vehicle_breakdown"]))
        updated["vehicle_breakdown"] = updated["customer_code"].map(bd_map).fillna("")
    else:
        # Build from scratch with all columns matching customer_master format
        updated = cust_zoned[[
            "customer_code", "customer_name", "street",
            "latitude", "longitude", "zone",
            "visits", "avg_weight_kg", "avg_cases",
            "preferred_vehicle", "eligible_vehicles", "vehicle_breakdown",
        ]].copy()
        updated["special_zone"]      = cust_zoned["special_zone"].fillna("")
        updated["time_window_start"] = "06:00"
        updated["time_window_end"]   = "18:00"
        updated["kg_per_case"]       = (
            cust_zoned["avg_weight_kg"] / cust_zoned["avg_cases"].replace(0, np.nan)
        ).fillna(0).round(3)
        updated["notes"]             = ""

    # ── 8. Build interactive map ───────────────────────────────────────────────
    map_html = _build_zone_map(cust_zoned, zone_summary, fl, zone_vehicle_map,
                               fleet_recommendation if mode == "auto" else None)

    # ── Improvement 6: Zone quality score (Fix 1: per-type CV) ─────────────────
    # Build per-type stop and weight arrays
    type_stop_cvs   = []
    type_weight_cvs = []
    zone_types = {z["zone"]: z["vehicle_type"] for z in zone_summary}
    all_vtypes = set(zone_types.values())
    for vt in all_vtypes:
        vt_zones = [z for z in zone_summary if z["vehicle_type"] == vt]
        if len(vt_zones) < 2:
            continue
        sv = np.array([z["exp_daily_stops"] for z in vt_zones])
        wv = np.array([z["exp_daily_kg"]    for z in vt_zones])
        type_stop_cvs.append(sv.std()  / max(sv.mean(),  1e-9))
        type_weight_cvs.append(wv.std() / max(wv.mean(), 1e-9))

    stop_cv   = float(np.mean(type_stop_cvs))   if type_stop_cvs   else 1.0
    weight_cv = float(np.mean(type_weight_cvs)) if type_weight_cvs else 1.0

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

    return updated, zone_summary, map_html, quality, _auto_recommendation


def _empty_suggestion(unclaimed=None):
    unclaimed = unclaimed or []
    return {
        "vehicle_zone_map":      {},
        "zone_vehicle_map":      {z: None for z in unclaimed},
        "unclaimed_zones":       list(unclaimed),
        "cross_type_assists":    [],
        "zone_assignments":      [],
        "special_zone_vehicles": {},
    }


def suggest_fleet_assignment(updated: pd.DataFrame, zone_summary: list,
                             fleet_df: pd.DataFrame, cross_type: bool = True,
                             cross_type_eligible_pct: float | None = None) -> dict:
    """
    Greedy, capacitated, nearest-neighbour suggestion for which vehicle should
    cover each zone that build_zones() left without a dedicated vehicle. A
    vehicle can be suggested for multiple zones (packed busiest-first, capped
    by its daily_cap_kg and MAX_STOPS_PER_DAY). When cross_type, a zone whose
    own vehicle type is out of capacity can be offered to a different type,
    but only if enough of that zone's customers list it in eligible_vehicles.
    Pure recommendation — does not modify zones or touch the live solver.
    """
    # ── Setup: normalize fleet, re-derive special-vehicle exclusion ──
    fl = fleet_df.copy()
    fl.columns = fl.columns.str.strip().str.lower()
    fl["vehicle_name"]      = fl["vehicle_name"].astype(str).str.strip()
    fl["vehicle_type"]      = fl["vehicle_type"].astype(str).str.strip()
    fl["capacity_kg"]       = pd.to_numeric(fl["capacity_kg"],       errors="coerce").fillna(3_200)
    fl["max_trips_per_day"] = pd.to_numeric(fl["max_trips_per_day"], errors="coerce").fillna(2).astype(int)
    fl["daily_cap_kg"]      = fl["capacity_kg"] * fl["max_trips_per_day"]

    fleet_vehicle_names = set(fl["vehicle_name"])
    special_veh_names, special_zone_vehicles = set(), {}
    for sz_name, sz_def in SPECIAL_ZONES.items():
        ptype = sz_def.get("primary_vehicle", "Van")
        match = fl[fl["vehicle_type"] == ptype].head(1)
        if not match.empty:
            vname = match.iloc[0]["vehicle_name"]
            special_veh_names.add(vname)
            special_zone_vehicles[vname] = sz_name
    fl_candidates = fl[~fl["vehicle_name"].isin(special_veh_names)].copy()
    threshold = (cross_type_eligible_pct if cross_type_eligible_pct is not None
                 else ZONE_CROSS_TYPE_ELIGIBLE_PCT) / 100.0

    # ── Zone records: skip already-resolved zones (special zones + fleet-mode 1:1) ──
    zone_records = []
    for z in zone_summary:
        if z["zone"] in fleet_vehicle_names:
            continue
        zone_records.append({
            "zone": z["zone"], "home_type": z["vehicle_type"],
            "centroid_lat": float(z["centroid_lat"]), "centroid_lon": float(z["centroid_lon"]),
            "p75_stops": float(z.get("p75_daily_stops", 0) or 0),
            "p75_kg":    float(z.get("p75_daily_kg", 0) or 0),
        })
    if not zone_records:
        return _empty_suggestion()

    # ── Vehicle records ──
    vehicles = {
        row["vehicle_name"]: {
            "vehicle_type": row["vehicle_type"], "daily_cap_kg": float(row["daily_cap_kg"]),
            "used_kg": 0.0, "used_stops": 0.0,
            "anchor_lat": DEPOT_LAT, "anchor_lon": DEPOT_LON, "anchor_weight": 0.0,
            "assigned_zones": [],
        }
        for _, row in fl_candidates.iterrows()
    }
    if not vehicles:
        return _empty_suggestion(unclaimed=[z["zone"] for z in zone_records])

    # ── Cross-type eligibility precompute (once, not per zone/type) ──
    eligible_sets_by_zone = {}
    if cross_type:
        from collections import defaultdict
        eligible_sets_by_zone = defaultdict(list)
        needed = {zr["zone"] for zr in zone_records}
        upd = updated.copy()
        upd.columns = upd.columns.str.strip().str.lower()
        for _, row in upd.iterrows():
            z = row.get("zone")
            if z not in needed:
                continue
            raw = row.get("eligible_vehicles")
            elig = ({x.strip() for x in str(raw).split(",") if x.strip()}
                    if not pd.isna(raw) and str(raw).strip()
                    else set(config.NEW_CUSTOMER_DEFAULT_VEHICLES))
            eligible_sets_by_zone[z].append(elig)

    def eligible_fraction(zone_name, candidate_type):
        sets = eligible_sets_by_zone.get(zone_name, [])
        return (sum(1 for s in sets if candidate_type in s) / len(sets)) if sets else 0.0

    def pick_nearest(zone_rec, candidate_names):
        feasible = []
        for vn in candidate_names:
            v = vehicles[vn]
            if v["used_kg"] + zone_rec["p75_kg"] > v["daily_cap_kg"]:
                continue
            if v["used_stops"] + zone_rec["p75_stops"] > MAX_STOPS_PER_DAY:
                continue
            d = haversine_km(v["anchor_lat"], v["anchor_lon"],
                              zone_rec["centroid_lat"], zone_rec["centroid_lon"])
            feasible.append((d, -(v["daily_cap_kg"] - v["used_kg"]), len(v["assigned_zones"]), vn))
        if not feasible:
            return None
        feasible.sort()
        return feasible[0][3]

    assignments = []

    def assign(zone_rec, vname, pass_no, cross=False, eligible_pct=None):
        v = vehicles[vname]
        d = haversine_km(v["anchor_lat"], v["anchor_lon"],
                          zone_rec["centroid_lat"], zone_rec["centroid_lon"])
        v["used_kg"] += zone_rec["p75_kg"]
        v["used_stops"] += zone_rec["p75_stops"]
        v["assigned_zones"].append(zone_rec["zone"])
        # incremental workload-weighted running centroid; 1kg floor avoids
        # divide-by-zero / a frozen anchor on a zero-weight ("very light") zone
        w = max(zone_rec["p75_kg"], 1.0)
        v["anchor_lat"] = (v["anchor_lat"] * v["anchor_weight"] + zone_rec["centroid_lat"] * w) / (v["anchor_weight"] + w)
        v["anchor_lon"] = (v["anchor_lon"] * v["anchor_weight"] + zone_rec["centroid_lon"] * w) / (v["anchor_weight"] + w)
        v["anchor_weight"] += w
        assignments.append({
            "zone": zone_rec["zone"], "home_type": zone_rec["home_type"],
            "vehicle": vname, "vehicle_type": v["vehicle_type"],
            "pass": pass_no, "cross_type": cross,
            "eligible_pct": round(eligible_pct, 1) if eligible_pct is not None else None,
            "distance_km": round(d, 2), "p75_kg": zone_rec["p75_kg"],
        })

    # ── Pass 1: same-type, busiest zones first ──
    for vtype in ["Kamion", "Furgon", "Van"]:
        zones_t = sorted((zr for zr in zone_records if zr["home_type"] == vtype),
                         key=lambda z: (-z["p75_stops"], -z["p75_kg"], z["zone"]))
        candidates_t = [vn for vn, v in vehicles.items() if v["vehicle_type"] == vtype]
        for zr in zones_t:
            picked = pick_nearest(zr, candidates_t)
            if picked:
                assign(zr, picked, pass_no=1)

    # ── Pass 2: cross-type, remaining zones compete for a shared pool ──
    if cross_type:
        assigned = {a["zone"] for a in assignments}
        remaining = sorted((zr for zr in zone_records if zr["zone"] not in assigned),
                           key=lambda z: (-z["p75_stops"], -z["p75_kg"], z["zone"]))
        for zr in remaining:
            qualifying = [t for t in ("Kamion", "Furgon", "Van")
                         if t != zr["home_type"] and eligible_fraction(zr["zone"], t) >= threshold]
            if not qualifying:
                continue
            pool = [vn for vn, v in vehicles.items() if v["vehicle_type"] in qualifying]
            picked = pick_nearest(zr, pool)
            if picked:
                assign(zr, picked, pass_no=2, cross=True,
                       eligible_pct=eligible_fraction(zr["zone"], vehicles[picked]["vehicle_type"]) * 100)

    # ── Build return, sorting each vehicle's zones busiest-first (for "primary zone" use) ──
    assigned_names = {a["zone"] for a in assignments}
    unclaimed_zones = sorted(z["zone"] for z in zone_records if z["zone"] not in assigned_names)
    vehicle_zone_map = {}
    for a in sorted(assignments, key=lambda a: -a["p75_kg"]):
        vehicle_zone_map.setdefault(a["vehicle"], []).append(a["zone"])
    zone_vehicle_map = {a["zone"]: a["vehicle"] for a in assignments}
    for z in unclaimed_zones:
        zone_vehicle_map[z] = None

    return {
        "vehicle_zone_map":      vehicle_zone_map,
        "zone_vehicle_map":      zone_vehicle_map,
        "unclaimed_zones":       unclaimed_zones,
        "cross_type_assists":    [a for a in assignments if a["cross_type"]],
        "zone_assignments":      assignments,
        "special_zone_vehicles": special_zone_vehicles,
    }


# ── Map builder ────────────────────────────────────────────────────────────────

_PALETTE = [
    "#1A5276","#1E8449","#D35400","#8E44AD","#C0392B","#17A589",
    "#B7950B","#2980B9","#27AE60","#E67E22","#6C3483","#E74C3C",
    "#1ABC9C","#F39C12","#5D6D7E","#A93226","#117A65","#CA6F1E",
    "#7D3C98","#154360","#0E6655","#BA4A00","#1F618D","#52BE80",
]


def _build_zone_map(cust_df: pd.DataFrame, zone_summary: list,
                    fleet_df: pd.DataFrame, zone_vehicle_map: dict | None = None,
                    fleet_recommendation: dict | None = None) -> str:
    """Build a self-contained Leaflet HTML map showing zones + customers."""

    # Assign colours per zone
    all_zones   = sorted(cust_df["zone"].dropna().unique())
    zone_colour = {z: _PALETTE[i % len(_PALETTE)] for i, z in enumerate(all_zones)}

    # Zone/vehicle coverage section (auto mode only)
    zone_vehicle_map = zone_vehicle_map or {}
    coverage_rows = ""
    if fleet_recommendation:
        for vtype, rec in fleet_recommendation.items():
            unclaimed_zones = [z for z in all_zones
                               if z.startswith(f"{vtype}_") and z not in zone_vehicle_map]
            if unclaimed_zones:
                coverage_rows += (
                    f"<div class='cr' style='color:#E74C3C'>{vtype}: "
                    f"{len(unclaimed_zones)} zone(s) unclaimed (Float-served)</div>"
                )
            if rec.get("spare_float", 0) > 0:
                coverage_rows += (
                    f"<div class='cr' style='color:#7F8C8D'>{vtype}: "
                    f"{rec['spare_float']} spare Float vehicle(s)</div>"
                )
        if not coverage_rows:
            coverage_rows = "<div class='cr' style='color:#7F8C8D'>All zones have a dedicated vehicle.</div>"

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
            f"<td>{z['exp_daily_stops']} / <b>{z['p75_daily_stops']}</b></td>"
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
    {"<h2>Zone / Vehicle Coverage</h2><div class='fs' style='font-size:10px;color:#95A5A6;padding:4px 0 6px'>Zones without a dedicated vehicle are served dynamically by Float vehicles.</div><div class='fs'>" + coverage_rows + "</div>" if coverage_rows else ""}
  </div>
  <div id="stats">Showing <b id="vc">-</b> of <b id="tc">-</b> customers</div>
</div>
<div id="map"></div>
</div>
<div id="bottom">
  <table>
    <tr>
      <th>Zone</th><th>Type</th><th>Customers</th>
      <th>Avg / P75 Stops/Day</th><th>Avg KG/Day</th>
      <th>Trip Cap (kg)</th><th>P75 Utilisation</th><th>Status</th>
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
