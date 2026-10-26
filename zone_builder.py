"""Deterministic, history-based freight zone creation."""

from __future__ import annotations

import io
import json
import re
from collections import Counter

import numpy as np
import pandas as pd
from openpyxl.styles import Font, PatternFill
from sklearn.cluster import KMeans

import config


class ZoneInputError(ValueError):
    """Raised when the uploaded workbook cannot support reliable zone building."""


HISTORY_ALIASES = {
    "delivery_date": ("delivery_date", "date"),
    "shipment_id": ("shipment_id", "trip_id", "shipment", "trip"),
    "customer_id": ("customer_id", "customer_code", "customer"),
    "vehicle_type": ("vehicle_type", "vehicle"),
    "quantity_kg": ("quantity_kg", "total_weight", "kg", "weight_kg"),
    "cases": ("cases", "number_of_cases", "quantity_cases"),
    "vehicle_id": ("vehicle_id", "vehicle_name", "vehicle_plate"),
    "customer_name": ("customer_name", "name"),
    "delivery_status": ("delivery_status", "status"),
}
CUSTOMER_ALIASES = {
    "customer_id": ("customer_id", "customer_code", "customer"),
    "customer_name": ("customer_name", "name"),
    "address": ("address", "street"),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lon", "lng"),
    "allowed_vehicle_types": ("allowed_vehicle_types", "eligible_vehicles"),
    "service_minutes": ("service_minutes",),
}


def _normalise_columns(frame: pd.DataFrame, aliases: dict[str, tuple[str, ...]]) -> pd.DataFrame:
    result = frame.copy()
    result.columns = [
        re.sub(r"[^a-z0-9]+", "_", str(column).strip().lower()).strip("_")
        for column in result.columns
    ]
    rename = {}
    for canonical, choices in aliases.items():
        for choice in choices:
            if choice in result.columns:
                rename[choice] = canonical
                break
    return result.rename(columns=rename)


def _identifier(value) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, (int, np.integer)):
        return str(value)
    if isinstance(value, (float, np.floating)) and float(value).is_integer():
        return str(int(value))
    return str(value).strip()


def _required_columns(frame: pd.DataFrame, columns: list[str], sheet_name: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ZoneInputError(
            f"The {sheet_name} sheet is missing required column(s): "
            f"{', '.join(missing)}. Download the input template for the expected format."
        )


def _read_inputs(history: pd.DataFrame, customers: pd.DataFrame):
    deliveries = _normalise_columns(history, HISTORY_ALIASES)
    customer_rows = _normalise_columns(customers, CUSTOMER_ALIASES)
    _required_columns(
        deliveries,
        [
            "delivery_date", "shipment_id", "customer_id", "vehicle_id",
            "vehicle_type", "quantity_kg",
        ],
        "DeliveryHistory",
    )
    _required_columns(customer_rows, ["customer_id", "latitude", "longitude"], "Customers")
    if deliveries.empty:
        raise ZoneInputError("The DeliveryHistory sheet contains no delivery rows.")
    if customer_rows.empty:
        raise ZoneInputError("The Customers sheet contains no customer rows.")

    deliveries["source_row"] = np.arange(2, len(deliveries) + 2)
    deliveries["customer_id"] = deliveries["customer_id"].map(_identifier)
    deliveries["shipment_id"] = deliveries["shipment_id"].map(_identifier)
    deliveries["vehicle_type"] = deliveries["vehicle_type"].fillna("").astype(str).str.strip()
    deliveries["delivery_date"] = pd.to_datetime(deliveries["delivery_date"], errors="coerce").dt.normalize()
    deliveries["quantity_kg"] = pd.to_numeric(deliveries["quantity_kg"], errors="coerce")
    if "cases" not in deliveries:
        deliveries["cases"] = 0.0
    deliveries["cases"] = pd.to_numeric(deliveries["cases"], errors="coerce").fillna(0.0)
    deliveries["vehicle_id"] = deliveries["vehicle_id"].fillna("").map(_identifier)
    if "customer_name" not in deliveries:
        deliveries["customer_name"] = ""
    if "delivery_status" in deliveries:
        excluded_statuses = {
            "cancelled", "canceled", "undelivered", "failed", "returned", "rejected",
        }
        status = deliveries["delivery_status"].fillna("").astype(str).str.strip().str.lower()
        deliveries = deliveries[~status.isin(excluded_statuses)].copy()
        if deliveries.empty:
            raise ZoneInputError(
                "No completed delivery rows remain after excluding cancelled/failed records."
            )

    bad = (
        deliveries["delivery_date"].isna()
        | (deliveries["shipment_id"] == "")
        | (deliveries["vehicle_id"] == "")
        | (deliveries["customer_id"] == "")
        | (deliveries["vehicle_type"] == "")
        | deliveries["quantity_kg"].isna()
        | ~np.isfinite(deliveries["quantity_kg"])
        | (deliveries["quantity_kg"] < 0)
        | ~np.isfinite(deliveries["cases"])
        | (deliveries["cases"] < 0)
    )
    if bad.any():
        rows = ", ".join(map(str, deliveries.loc[bad, "source_row"].head(8)))
        raise ZoneInputError(
            f"DeliveryHistory has missing or invalid required values on Excel row(s) {rows}. "
            "Check dates, shipment/customer/vehicle IDs, vehicle types, and non-negative quantities."
        )

    customer_rows["customer_id"] = customer_rows["customer_id"].map(_identifier)
    customer_rows["latitude"] = pd.to_numeric(customer_rows["latitude"], errors="coerce")
    customer_rows["longitude"] = pd.to_numeric(customer_rows["longitude"], errors="coerce")
    if "service_minutes" in customer_rows:
        customer_rows["service_minutes"] = pd.to_numeric(
            customer_rows["service_minutes"], errors="coerce"
        )
        invalid_service = customer_rows["service_minutes"].notna() & (
            ~np.isfinite(customer_rows["service_minutes"])
            | (customer_rows["service_minutes"] < 0)
        )
        if invalid_service.any():
            raise ZoneInputError("Customers.service_minutes must be a non-negative number when provided.")
    invalid_coords = (
        customer_rows["customer_id"].eq("")
        | customer_rows["latitude"].isna()
        | customer_rows["longitude"].isna()
        | ~customer_rows["latitude"].between(-90, 90)
        | ~customer_rows["longitude"].between(-180, 180)
    )
    if invalid_coords.any():
        rows = ", ".join(map(str, customer_rows.index[invalid_coords][:8] + 2))
        raise ZoneInputError(
            f"Customers has missing or invalid IDs/coordinates on Excel row(s) {rows}."
        )
    if customer_rows["customer_id"].duplicated().any():
        duplicates = customer_rows.loc[
            customer_rows["customer_id"].duplicated(keep=False), "customer_id"
        ].unique()
        raise ZoneInputError(
            "Customers must contain one row per customer. Duplicate ID(s): "
            + ", ".join(duplicates[:8])
        )

    delivery_ids = set(deliveries["customer_id"])
    customer_ids = set(customer_rows["customer_id"])
    missing_customers = sorted(delivery_ids - customer_ids)
    if missing_customers:
        raise ZoneInputError(
            f"{len(missing_customers)} customer ID(s) in DeliveryHistory are missing from "
            "Customers, for example: " + ", ".join(missing_customers[:8])
        )

    shipment_key = ["delivery_date", "shipment_id"]
    type_counts = deliveries.groupby(shipment_key, dropna=False)["vehicle_type"].nunique()
    if (type_counts > 1).any():
        examples = deliveries.groupby(shipment_key)["vehicle_type"].nunique()
        examples = examples[examples > 1].index.tolist()[:5]
        formatted = ", ".join(f"{date.date()} / {shipment}" for date, shipment in examples)
        raise ZoneInputError(
            "A shipment must have one vehicle type. Conflicting types were found for: "
            f"{formatted}. Check that shipment_id identifies one vehicle trip."
        )

    if deliveries["vehicle_id"].ne("").any():
        vehicle_counts = deliveries[deliveries["vehicle_id"].ne("")].groupby(
            shipment_key
        )["vehicle_id"].nunique()
        if (vehicle_counts > 1).any():
            raise ZoneInputError(
                "A shipment_id/date pair is linked to multiple vehicle IDs. "
                "Use a distinct shipment_id for each vehicle trip."
            )

    stops = (
        deliveries.groupby(
            ["delivery_date", "shipment_id", "customer_id", "vehicle_type"],
            as_index=False,
            sort=True,
        )
        .agg(quantity_kg=("quantity_kg", "sum"), cases=("cases", "sum"))
    )
    return deliveries, customer_rows, stops


def _customer_profiles(deliveries: pd.DataFrame, customers: pd.DataFrame, stops: pd.DataFrame):
    dates = pd.DatetimeIndex(sorted(stops["delivery_date"].unique()))
    weekdays = sorted(set(dates.dayofweek))
    months = sorted(set(dates.month))
    date_counts_by_weekday = Counter(date.dayofweek for date in dates)
    date_counts_by_month = Counter(date.month for date in dates)

    type_counts = stops.groupby(["customer_id", "vehicle_type"]).size().unstack(fill_value=0)
    type_counts = type_counts.reindex(sorted(type_counts.columns), axis=1)
    type_counts.columns.name = None
    total_type_stops = type_counts.sum(axis=1)

    stop_pivot = stops.pivot_table(
        index="customer_id", columns="delivery_date", values="shipment_id", aggfunc="count", fill_value=0
    ).reindex(columns=dates, fill_value=0)
    kg_pivot = stops.pivot_table(
        index="customer_id", columns="delivery_date", values="quantity_kg", aggfunc="sum", fill_value=0
    ).reindex(columns=dates, fill_value=0)

    profiles = []
    customers = customers.set_index("customer_id", drop=False)
    for customer_id, customer in customers.iterrows():
        customer_stops = stops[stops["customer_id"] == customer_id]
        counts = type_counts.loc[customer_id] if customer_id in type_counts.index else pd.Series(dtype=float)
        if counts.sum() == 0:
            continue
        total_stops = int(counts.sum())
        kg_by_type = customer_stops.groupby("vehicle_type")["quantity_kg"].sum()
        total_kg = float(kg_by_type.sum())
        type_scores = {
            vehicle_type: 0.5 * (count / total_stops)
            + 0.5 * (float(kg_by_type.get(vehicle_type, 0)) / max(total_kg, 1.0))
            for vehicle_type, count in counts.items()
        }
        preferred_type = sorted(
            type_scores, key=lambda vehicle_type: (-type_scores[vehicle_type], -counts[vehicle_type], vehicle_type)
        )[0]
        type_mix = " | ".join(
            f"{vehicle_type}: {100 * count / total_stops:.1f}% stops, "
            f"{100 * float(kg_by_type.get(vehicle_type, 0)) / max(total_kg, 1.0):.1f}% kg"
            for vehicle_type, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
            if count
        )
        type_workload = " | ".join(
            f"{vehicle_type}: {int(count)} stops, "
            f"{customer_stops.loc[customer_stops['vehicle_type'] == vehicle_type, 'quantity_kg'].sum():.0f} kg"
            for vehicle_type, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
            if count
        )

        customer_dates = customer_stops["delivery_date"]
        raw_allowed = customer.get("allowed_vehicle_types", "")
        explicit_restriction = pd.notna(raw_allowed) and bool(str(raw_allowed).strip())
        customer_name = customer.get("customer_name", "")
        address = customer.get("address", "")
        profile = {
            "customer_id": customer_id,
            "customer_name": "" if pd.isna(customer_name) else str(customer_name).strip(),
            "address": "" if pd.isna(address) else str(address).strip(),
            "latitude": float(customer["latitude"]),
            "longitude": float(customer["longitude"]),
            "visits": total_stops,
            "observed_days": int(customer_dates.nunique()),
            "p75_daily_stops": float(
                stop_pivot.loc[customer_id].reindex(dates, fill_value=0).quantile(0.75)
            ),
            "p75_daily_kg": float(
                kg_pivot.loc[customer_id].reindex(dates, fill_value=0).quantile(0.75)
            ),
            "total_kg": float(customer_stops["quantity_kg"].sum()),
            "avg_weight_kg": float(customer_stops["quantity_kg"].mean()),
            "avg_cases": float(customer_stops["cases"].mean()),
            "preferred_vehicle": preferred_type,
            "vehicle_type_confidence": (
                "High" if total_stops >= 20 and type_scores[preferred_type] >= 0.70
                else "Medium" if total_stops >= 10
                else "Low"
            ),
            "vehicle_breakdown": type_mix,
            "vehicle_type_workload": type_workload,
            "historical_vehicle_types": ",".join(
                sorted(str(v) for v in counts.index if counts[v] > 0)
            ),
            "eligible_vehicles": "",
            "explicit_vehicle_restriction": explicit_restriction,
            "historical_type_counts": counts.to_dict(),
            "special_zone": "",
        }
        allowed = []
        if explicit_restriction:
            allowed = [part.strip() for part in re.split(r"[,;|]", str(raw_allowed)) if part.strip()]
            unknown = sorted(set(allowed) - set(counts.index) - set(config.TRIP_CAPACITY))
            if unknown:
                raise ZoneInputError(
                    f"Customer {customer_id} lists unknown allowed vehicle type(s): "
                    f"{', '.join(unknown)}."
                )
            if preferred_type not in allowed:
                preferred_type = sorted(
                    allowed,
                    key=lambda v: (-type_scores.get(v, 0), -counts.get(v, 0), v),
                )[0]
                profile["preferred_vehicle"] = preferred_type
        else:
            # History indicates observed usage, not a hard restriction. Keep the
            # routing eligibility broad unless an explicit customer constraint exists.
            allowed = sorted(set(config.TRIP_CAPACITY) | set(counts.index))
        if not allowed:
            raise ZoneInputError(f"Customer {customer_id} has no allowed vehicle types.")
        profile["eligible_vehicles"] = ",".join(allowed)

        stop_series = stop_pivot.loc[customer_id] if customer_id in stop_pivot.index else pd.Series(0, index=dates)
        kg_series = kg_pivot.loc[customer_id] if customer_id in kg_pivot.index else pd.Series(0, index=dates)
        for weekday in weekdays:
            dates_for_type = [date for date in dates if date.dayofweek == weekday]
            profile[f"weekday_stops_{weekday}"] = (
                float(stop_series.reindex(dates_for_type, fill_value=0).sum())
                / max(1, date_counts_by_weekday[weekday])
            )
            profile[f"weekday_kg_{weekday}"] = (
                float(kg_series.reindex(dates_for_type, fill_value=0).sum())
                / max(1, date_counts_by_weekday[weekday])
            )
        for month in months:
            dates_for_type = [date for date in dates if date.month == month]
            profile[f"month_stops_{month}"] = (
                float(stop_series.reindex(dates_for_type, fill_value=0).sum())
                / max(1, date_counts_by_month[month])
            )
            profile[f"month_kg_{month}"] = (
                float(kg_series.reindex(dates_for_type, fill_value=0).sum())
                / max(1, date_counts_by_month[month])
            )
        profiles.append(profile)

    profile_df = pd.DataFrame(profiles)
    if profile_df.empty:
        raise ZoneInputError("No customers with valid delivery stops were found.")
    return profile_df, dates, weekdays, months


def _allocate_zone_counts(groups: dict[str, pd.DataFrame], requested: int) -> dict[str, int]:
    names = sorted(groups)
    total_customers = sum(len(groups[name]) for name in names)
    zone_count = min(max(requested, len(names)), total_customers)
    allocations = {name: 1 for name in names}
    remaining = zone_count - len(names)
    if remaining <= 0:
        return allocations
    weights = {
        name: max(
            1.0,
            float(groups[name]["p75_daily_stops"].sum())
            + float(groups[name]["p75_daily_kg"].sum())
            / max(1.0, float(groups[name]["avg_weight_kg"].median())),
        )
        for name in names
    }
    weight_total = sum(weights.values())
    exact = {name: remaining * weights[name] / weight_total for name in names}
    for name in names:
        extra = min(len(groups[name]) - 1, int(np.floor(exact[name])))
        allocations[name] += extra
    left = zone_count - sum(allocations.values())
    order = sorted(names, key=lambda name: (-(exact[name] % 1), name))
    while left:
        changed = False
        for name in order:
            if allocations[name] < len(groups[name]):
                allocations[name] += 1
                left -= 1
                changed = True
                if left == 0:
                    break
        if not changed:
            break
    return allocations


def _cluster_group(frame: pd.DataFrame, count: int) -> pd.Series:
    if count >= len(frame):
        return pd.Series(range(len(frame)), index=frame.index)
    coordinates = frame[["latitude", "longitude"]].to_numpy(dtype=float)
    kg_scale = max(1.0, float(frame["avg_weight_kg"].median()))
    demand_weight = np.sqrt(
        1.0 + frame["p75_daily_stops"].to_numpy(dtype=float)
        + frame["p75_daily_kg"].to_numpy(dtype=float) / kg_scale
    )
    model = KMeans(n_clusters=count, random_state=42, n_init=20, max_iter=500)
    labels = model.fit_predict(coordinates, sample_weight=demand_weight)
    return pd.Series(_balance_clusters(frame, labels), index=frame.index)


def _balance_clusters(frame: pd.DataFrame, labels: np.ndarray) -> np.ndarray:
    """Make small, nearby transfers to reduce peak stop/weight imbalance."""
    labels = np.asarray(labels, dtype=int).copy()
    cluster_ids = sorted(set(labels))
    if len(cluster_ids) < 2 or len(frame) < 4:
        return labels

    coordinates = frame[["latitude", "longitude"]].to_numpy(dtype=float)
    stop_load = frame["p75_daily_stops"].to_numpy(dtype=float)
    kg_load = frame["p75_daily_kg"].to_numpy(dtype=float)
    scenario_columns = [
        column for column in frame.columns
        if column.startswith(("weekday_stops_", "weekday_kg_", "month_stops_", "month_kg_"))
    ]
    scenario_loads = (
        frame[scenario_columns].to_numpy(dtype=float)
        if scenario_columns else np.zeros((len(frame), 0), dtype=float)
    )
    counts = {cluster: int(np.sum(labels == cluster)) for cluster in cluster_ids}
    stop_totals = {cluster: float(stop_load[labels == cluster].sum()) for cluster in cluster_ids}
    kg_totals = {cluster: float(kg_load[labels == cluster].sum()) for cluster in cluster_ids}
    scenario_totals = {
        cluster: scenario_loads[labels == cluster].sum(axis=0)
        for cluster in cluster_ids
    }

    def centers():
        return {
            cluster: coordinates[labels == cluster].mean(axis=0)
            for cluster in cluster_ids
            if counts[cluster]
        }

    def balance_cost(totals):
        values = np.array([totals[cluster] for cluster in cluster_ids], dtype=float)
        return float((values.std() / max(values.mean(), 1e-9)) ** 2)

    def scenario_cost(totals):
        if scenario_loads.shape[1] == 0:
            return 0.0
        values = np.stack([totals[cluster] for cluster in cluster_ids])
        means = values.mean(axis=0)
        cvs = np.divide(
            values.std(axis=0),
            np.maximum(means, 1e-9),
            out=np.zeros_like(means),
            where=means > 1e-9,
        )
        return float(np.mean(cvs ** 2))

    for _ in range(3):
        changed = False
        centers_now = centers()
        order = np.argsort(-(stop_load + kg_load / max(1.0, np.median(kg_load))))
        for customer_index in order:
            source = int(labels[customer_index])
            if counts[source] <= 1:
                continue
            source_center = centers_now[source]
            source_distance = haversine_km(
                *coordinates[customer_index], *source_center
            )
            nearby = sorted(
                (
                    haversine_km(*coordinates[customer_index], *center),
                    cluster,
                )
                for cluster, center in centers_now.items()
                if cluster != source
            )[:4]
            best = None
            current_cost = (
                0.20 * balance_cost(stop_totals)
                + 0.10 * balance_cost(kg_totals)
                + 0.30 * scenario_cost(scenario_totals)
            )
            for distance, target in nearby:
                if distance > source_distance + 0.75:
                    continue
                new_stops = dict(stop_totals)
                new_kg = dict(kg_totals)
                new_stops[source] -= stop_load[customer_index]
                new_stops[target] += stop_load[customer_index]
                new_kg[source] -= kg_load[customer_index]
                new_kg[target] += kg_load[customer_index]
                new_scenarios = dict(scenario_totals)
                new_scenarios[source] = (
                    scenario_totals[source] - scenario_loads[customer_index]
                )
                new_scenarios[target] = (
                    scenario_totals[target] + scenario_loads[customer_index]
                )
                geo_delta = (distance ** 2 - source_distance ** 2) / max(1, len(frame)) / 25
                candidate_cost = (
                    0.20 * balance_cost(new_stops)
                    + 0.10 * balance_cost(new_kg)
                    + 0.30 * scenario_cost(new_scenarios)
                    + 0.40 * geo_delta
                )
                if candidate_cost < current_cost - 1e-9 and (
                    best is None or candidate_cost < best[0]
                ):
                    best = (candidate_cost, target)
            if best is not None:
                target = best[1]
                labels[customer_index] = target
                counts[source] -= 1
                counts[target] += 1
                stop_totals[source] -= stop_load[customer_index]
                stop_totals[target] += stop_load[customer_index]
                kg_totals[source] -= kg_load[customer_index]
                kg_totals[target] += kg_load[customer_index]
                scenario_totals[source] -= scenario_loads[customer_index]
                scenario_totals[target] += scenario_loads[customer_index]
                changed = True
        if not changed:
            break
    return labels


def _apply_special_zones(profiles: pd.DataFrame) -> pd.DataFrame:
    result = profiles.copy()
    for index, row in result.iterrows():
        for zone_name, definition in config.SPECIAL_ZONES.items():
            polygon = definition.get("polygon", [])
            if polygon and _point_in_polygon(row["latitude"], row["longitude"], polygon):
                result.at[index, "special_zone"] = zone_name
                break
    return result


def _point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    inside = False
    x, y = lon, lat
    j = len(polygon) - 1
    for i, (yi, xi) in enumerate(polygon):
        yj, xj = polygon[j]
        if (yi > y) != (yj > y):
            cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < cross:
                inside = not inside
        j = i
    return inside


def _zone_summaries(profiles: pd.DataFrame, stops: pd.DataFrame, dates: pd.DatetimeIndex) -> list[dict]:
    summaries = []
    for zone_name, members in profiles.groupby("zone", sort=True):
        customer_ids = set(members["customer_id"])
        zone_stops = stops[stops["customer_id"].isin(customer_ids)].copy()
        daily = zone_stops.groupby("delivery_date").agg(
            stops=("shipment_id", "count"), kg=("quantity_kg", "sum")
        ).reindex(dates, fill_value=0)
        weekday_load = zone_stops.assign(
            weekday=zone_stops["delivery_date"].dt.day_name()
        ).groupby("weekday").agg(
            stops=("shipment_id", "count"), kg=("quantity_kg", "sum")
        )
        month_load = zone_stops.assign(
            month=zone_stops["delivery_date"].dt.strftime("%B")
        ).groupby("month").agg(
            stops=("shipment_id", "count"), kg=("quantity_kg", "sum")
        )
        date_weekdays = pd.Series(dates.day_name(), index=dates)
        date_months = pd.Series(dates.strftime("%B"), index=dates)
        daily_weekday_stops = daily["stops"].groupby(date_weekdays).mean()
        daily_weekday_kg = daily["kg"].groupby(date_weekdays).mean()
        daily_month_stops = daily["stops"].groupby(date_months).mean()
        daily_month_kg = daily["kg"].groupby(date_months).mean()
        type_stop_counts = zone_stops["vehicle_type"].value_counts()
        total_stops = int(type_stop_counts.sum())
        type_kg = zone_stops.groupby("vehicle_type")["quantity_kg"].sum()
        total_kg = max(float(type_kg.sum()), 1.0)
        type_scores = {
            vehicle_type: 0.5 * (type_stop_counts[vehicle_type] / max(1, total_stops))
            + 0.5 * (float(type_kg.get(vehicle_type, 0)) / total_kg)
            for vehicle_type in type_stop_counts.index
        }
        primary_type = sorted(
            type_scores, key=lambda value: (-type_scores[value], -type_stop_counts[value], value)
        )[0]
        primary_share = float(type_scores[primary_type])
        type_mix = " | ".join(
            f"{vehicle_type}: {100 * type_stop_counts[vehicle_type] / max(1, total_stops):.1f}% stops, "
            f"{100 * float(type_kg.get(vehicle_type, 0)) / total_kg:.1f}% kg"
            for vehicle_type in type_stop_counts.index
        )
        type_kg_mix = " | ".join(
            f"{vehicle_type}: {100 * quantity / total_kg:.1f}% kg"
            for vehicle_type, quantity in sorted(type_kg.items(), key=lambda pair: (-pair[1], pair[0]))
        )
        confidence = (
            "High" if total_stops >= 20 and primary_share >= 0.70
            else "Medium" if total_stops >= 10
            else "Low"
        )
        summary = {
            "zone": zone_name,
            "customers": int(len(members)),
            "observed_stops": total_stops,
            "avg_stops_per_observed_day": round(float(daily["stops"].mean()), 2),
            "p75_daily_stops": round(float(daily["stops"].quantile(0.75)), 2),
            "avg_kg_per_observed_day": round(float(daily["kg"].mean()), 1),
            "p75_daily_kg": round(float(daily["kg"].quantile(0.75)), 1),
            "primary_vehicle_type": primary_type,
            "vehicle_type_mix": type_mix,
            "vehicle_type_kg_mix": type_kg_mix,
            "type_confidence": confidence,
            "busiest_weekday": (
                f"{weekday_load['stops'].idxmax()} ({int(weekday_load['stops'].max())} stops)"
                if not weekday_load.empty else "—"
            ),
            "busiest_observed_month": (
                f"{month_load['stops'].idxmax()} ({int(month_load['stops'].max())} stops)"
                if not month_load.empty else "—"
            ),
        }
        for weekday, value in daily_weekday_stops.items():
            summary[f"avg_{weekday}_stops_per_day"] = round(float(value), 2)
            summary[f"avg_{weekday}_kg_per_day"] = round(
                float(daily_weekday_kg.get(weekday, 0)), 1
            )
        for month, value in daily_month_stops.items():
            summary[f"avg_{month}_stops_per_day"] = round(float(value), 2)
            summary[f"avg_{month}_kg_per_day"] = round(
                float(daily_month_kg.get(month, 0)), 1
            )
        summaries.append(summary)
    return summaries


def build_zone_proposal(
    history_df: pd.DataFrame,
    customers_df: pd.DataFrame,
    target_zone_count: int = 32,
) -> tuple[pd.DataFrame, list[dict], dict]:
    """Build stable customer zones from historical shipments and customer data."""
    if not 1 <= target_zone_count <= 100:
        raise ZoneInputError("Target zone count must be between 1 and 100.")
    deliveries, customers, stops = _read_inputs(history_df, customers_df)
    profiles, dates, weekdays, months = _customer_profiles(deliveries, customers, stops)
    profiles = _apply_special_zones(profiles)
    for index, row in profiles[profiles["special_zone"].astype(bool)].iterrows():
        special_allowed = set(
            config.SPECIAL_ZONES[row["special_zone"]].get("allowed_vehicles", [])
        )
        if row["explicit_vehicle_restriction"]:
            current_allowed = set(row["eligible_vehicles"].split(","))
            special_allowed &= current_allowed
        if not special_allowed:
            raise ZoneInputError(
                f"Customer {row['customer_id']} has no vehicle type compatible with "
                f"the special-zone rules for {row['special_zone']}."
            )
        profiles.at[index, "eligible_vehicles"] = ",".join(sorted(special_allowed))
        if row["preferred_vehicle"] not in special_allowed:
            counts = row["historical_type_counts"]
            profiles.at[index, "preferred_vehicle"] = sorted(
                special_allowed, key=lambda vehicle_type: (-counts.get(vehicle_type, 0), vehicle_type)
            )[0]
    for column in profiles.columns:
        if column.startswith(("weekday_stops_", "weekday_kg_", "month_stops_", "month_kg_")):
            profiles[column] = profiles[column].astype(float)

    special_mask = profiles["special_zone"].astype(bool)
    if special_mask.any():
        profiles.loc[special_mask, "zone"] = profiles.loc[special_mask, "special_zone"]

    regular = profiles[~special_mask].copy()
    groups = {
        str(vehicle_type): group.copy()
        for vehicle_type, group in regular.groupby("preferred_vehicle", sort=True)
    }
    special_zone_count = profiles.loc[special_mask, "special_zone"].nunique()
    requested_regular = max(0, target_zone_count - int(special_zone_count))
    effective_regular = max(requested_regular, len(groups))
    effective_regular = min(effective_regular, len(regular))
    counts = _allocate_zone_counts(groups, effective_regular) if groups else {}

    next_zone_number = Counter()
    for vehicle_type in sorted(groups):
        group = groups[vehicle_type].copy().sort_values("customer_id").reset_index()
        n_clusters = counts[vehicle_type]
        labels = _cluster_group(group, n_clusters)
        group["cluster_label"] = labels.to_numpy()
        for label in sorted(group["cluster_label"].unique()):
            next_zone_number[vehicle_type] += 1
            zone_name = f"{vehicle_type}_{next_zone_number[vehicle_type]:02d}"
            customer_indices = group.loc[group["cluster_label"] == label, "index"]
            profiles.loc[customer_indices, "zone"] = zone_name

    if profiles["zone"].isna().any():
        missing = profiles.loc[profiles["zone"].isna(), "customer_id"].head(8).tolist()
        raise ZoneInputError("Could not assign a zone to every customer: " + ", ".join(missing))

    profiles["visits"] = profiles["visits"].astype(int)
    profiles["special_zone"] = profiles["special_zone"].fillna("")
    profiles["zone"] = profiles["zone"].astype(str)
    profiles["time_window_start"] = profiles["special_zone"].map(
        {name: definition.get("time_window_start", "06:00")
         for name, definition in config.SPECIAL_ZONES.items()}
    ).fillna("06:00")
    profiles["time_window_end"] = profiles["special_zone"].map(
        {name: definition.get("time_window_end", "18:00")
         for name, definition in config.SPECIAL_ZONES.items()}
    ).fillna("18:00")
    profiles["service_minutes"] = customers.set_index("customer_id").reindex(
        profiles["customer_id"]
    ).get("service_minutes", pd.Series(index=profiles.index, dtype=float)).to_numpy()

    keep_columns = [
        "customer_id", "customer_name", "address", "latitude", "longitude",
        "zone", "special_zone", "eligible_vehicles", "preferred_vehicle",
        "vehicle_type_confidence", "historical_vehicle_types", "vehicle_breakdown",
        "visits", "observed_days",
        "vehicle_type_workload", "avg_weight_kg", "avg_cases", "total_kg",
        "p75_daily_stops", "p75_daily_kg", "service_minutes",
        "time_window_start", "time_window_end",
    ]
    updated = profiles[keep_columns].rename(columns={
        "customer_id": "customer_code",
        "address": "street",
    }).copy()
    updated["customer_name"] = updated["customer_name"].replace("", np.nan).fillna(
        updated["customer_code"]
    )
    updated["notes"] = ""

    summaries = _zone_summaries(profiles, stops, dates)
    adjusted_target = len(summaries)
    messages = []
    if adjusted_target != target_zone_count:
        messages.append(
            f"Requested {target_zone_count} zones; created {adjusted_target} to ensure "
            "each vehicle-type group and special area has a zone."
        )
    month_span = (dates[-1].to_period("M") - dates[0].to_period("M")).n + 1
    if month_span < 6:
        messages.append(
            f"History covers {month_span} calendar month(s). Workload balance is only "
            "validated for observed months; unobserved seasons are unknown."
        )
    low_history = int((profiles["visits"] < 5).sum())
    if low_history:
        messages.append(
            f"{low_history} customer(s) have fewer than five historical stops; "
            "their vehicle-type profile has low evidence."
        )
    no_history = len(customers) - len(profiles)
    if no_history:
        messages.append(
            f"{no_history} customer(s) in Customers had no completed history and were not included in the proposal."
        )

    metadata = {
        "requested_zone_count": target_zone_count,
        "created_zone_count": adjusted_target,
        "history_start": dates[0].strftime("%Y-%m-%d"),
        "history_end": dates[-1].strftime("%Y-%m-%d"),
        "observed_months": [pd.Timestamp(f"2000-{month:02d}-01").strftime("%B") for month in months],
        "observed_weekdays": [pd.Timestamp("2024-01-01") + pd.Timedelta(days=day) for day in weekdays],
        "delivery_rows": int(len(deliveries)),
        "stops": int(len(stops)),
        "customers": int(len(updated)),
        "messages": messages,
        "clustering_method": (
            "Deterministic K-means (random_state=42), grouped by customer primary type "
            "ranked from stop and kg shares; neighborhood transfers balance observed "
            "weekday/month demand while limiting geographic spread"
        ),
        "algorithm_version": "1.0",
    }
    metadata["observed_weekdays"] = [day.strftime("%A") for day in metadata["observed_weekdays"]]
    return updated, summaries, metadata


def make_zone_workbook(customers: pd.DataFrame, summaries: list[dict], metadata: dict) -> bytes:
    """Create an Excel workbook accepted by the route planner."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        customers.to_excel(writer, sheet_name="Customers", index=False)
        pd.DataFrame(summaries).to_excel(writer, sheet_name="Zone Summary", index=False)
        pd.DataFrame(
            [
                {
                    "item": key,
                    "value": json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (list, dict)) else value,
                }
                for key, value in metadata.items()
            ]
        ).to_excel(writer, sheet_name="Build Info", index=False)
    return buffer.getvalue()


def make_input_template() -> bytes:
    """Create a blank two-sheet workbook with the supported input schema."""
    buffer = io.BytesIO()
    history = pd.DataFrame(columns=[
        "delivery_date", "shipment_id", "customer_id", "vehicle_type", "quantity_kg",
        "cases", "vehicle_id", "delivery_status",
    ])
    customers = pd.DataFrame(columns=[
        "customer_id", "customer_name", "address", "latitude", "longitude",
        "allowed_vehicle_types", "service_minutes",
    ])
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        history.to_excel(writer, sheet_name="DeliveryHistory", index=False)
        customers.to_excel(writer, sheet_name="Customers", index=False)
        for sheet in writer.book.worksheets:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for cell in sheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill(fill_type="solid", fgColor="1F4E79")
    return buffer.getvalue()
