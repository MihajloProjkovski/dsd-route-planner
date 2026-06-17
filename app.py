"""
app.py - DSD Route Planner Web Application
Deployed on Streamlit Community Cloud.

Pages:
  Route Planning  - upload today.xlsx, select mode, generate routes, download output
  Admin           - password-protected: customer master, new customer, fleet/zones, config
"""

import io
import os
import sys
from datetime import date

import openpyxl
import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config
import route_planner as rp
import suggest_zones as sz

st.set_page_config(
    page_title="DSD Route Planner",
    page_icon="🚛",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
  .block-container { padding-top: 1.2rem; }
  .stButton > button { border-radius: 6px; }
  div[data-testid="metric-container"] { background: #f8f9fa; border-radius: 8px; padding: 8px; }
</style>
""", unsafe_allow_html=True)

st.sidebar.title("🚛 DSD Route Planner")
st.sidebar.caption(f"📅 {date.today().strftime('%A, %d %b %Y')}")
st.sidebar.markdown("---")
page = st.sidebar.radio(
    "Navigate",
    ["🗺 Route Planning", "⚙️ Admin"],
    label_visibility="collapsed",
)
st.sidebar.markdown("---")
st.sidebar.caption("v1.0 · Skopje DSD")


def ensure_today_xlsx():
    if not os.path.exists(config.TODAY_FILE):
        from openpyxl.styles import PatternFill, Font, Alignment
        type_fills = {
            "Kamion": PatternFill("solid", fgColor="D6E4F7"),
            "Furgon": PatternFill("solid", fgColor="D6F7E4"),
            "Van":    PatternFill("solid", fgColor="FFF3CD"),
        }
        HDR_FILL = PatternFill("solid", fgColor="1F4E79")
        HDR_FONT = Font(color="FFFFFF", bold=True)
        wb = openpyxl.Workbook()

        ws_ord = wb.active
        ws_ord.title = "Orders"
        ws_ord.append(["customer_code", "customer_name", "cases", "kg"])
        for cell in ws_ord[1]:
            cell.fill = HDR_FILL
            cell.font = HDR_FONT
            cell.alignment = Alignment(horizontal="center")

        ws_veh = wb.create_sheet("Vehicles")
        ws_veh.append(["vehicle_name", "vehicle_type", "zone",
                        "available", "max_trips_per_day", "notes"])
        for cell in ws_veh[1]:
            cell.fill = HDR_FILL
            cell.font = HDR_FONT
            cell.alignment = Alignment(horizontal="center")
        for v in config.FLEET:
            ws_veh.append([v["name"], v["type"], "", True, config.MAX_TRIPS_NORMAL, ""])
            fill = type_fills.get(v["type"])
            if fill:
                for cell in ws_veh[ws_veh.max_row]:
                    cell.fill = fill

        wb.save(config.TODAY_FILE)


def run_routing(file_bytes: bytes, mode: str):
    with open(config.TODAY_FILE, "wb") as f:
        f.write(file_bytes)

    stops_df    = rp.load_orders(config.TODAY_FILE, config.CUSTOMER_MASTER)
    vehicles_df = rp.load_vehicles(config.TODAY_FILE)

    zone_summary = None
    if mode == "territory":
        routes, unassigned, zone_summary = rp.solve_territory(stops_df, vehicles_df)
    else:
        routes, unassigned, stops_df = rp.solve(stops_df, vehicles_df)

    buf = io.BytesIO()
    rp.write_excel(routes, stops_df, unassigned, buf, zone_summary)
    buf.seek(0)

    return buf.getvalue(), routes, stops_df, unassigned, zone_summary


def check_zones_assigned(file_bytes: bytes) -> bool:
    try:
        xl  = pd.ExcelFile(io.BytesIO(file_bytes))
        veh = xl.parse("Vehicles")
        veh.columns = veh.columns.str.lower().str.strip()
        veh = veh.dropna(subset=["vehicle_name"])
        zones = veh["zone"].fillna("").astype(str).str.strip().str.lower()
        return any(z not in ("", "float", "nan", "none") for z in zones)
    except Exception:
        return False


# ── Page: Route Planning ──────────────────────────────────────────────────────
if page == "🗺 Route Planning":
    st.header("Route Planning")

    col_up, col_mode = st.columns([3, 1])

    with col_up:
        uploaded = st.file_uploader(
            "Upload **today.xlsx** (fill Orders sheet + check Vehicles sheet)",
            type=["xlsx"],
            help="The file must have an 'Orders' sheet and a 'Vehicles' sheet.",
        )

    with col_mode:
        st.markdown("**Routing Mode**")
        mode = st.radio(
            "mode",
            ["Territory", "Optimise"],
            label_visibility="collapsed",
            help=(
                "**Territory** — each vehicle serves its own zone. "
                "Load is rebalanced automatically. Best for normal and heavy days.\n\n"
                "**Optimise** — solver assigns stops freely. Best for light days."
            ),
        )

    if uploaded:
        file_bytes = uploaded.getvalue()

        try:
            prev = pd.read_excel(io.BytesIO(file_bytes), sheet_name="Orders")
            prev = prev.dropna(how="all")
            st.caption(f"📦 **{len(prev)}** orders detected in uploaded file")
        except Exception:
            pass

        if mode == "Territory" and not check_zones_assigned(file_bytes):
            st.warning(
                "⚠️ No zone assignments found in the Vehicles sheet. "
                "Go to **Admin → Fleet & Zones** to generate zone suggestions, "
                "or switch to **Optimise** mode."
            )

        if st.button("▶  Generate Routes", type="primary"):
            with st.spinner(
                f"Running {mode} routing... "
                f"{'(instant)' if mode == 'Territory' else '(up to 2 min for Optimise)'}"
            ):
                try:
                    excel_bytes, routes, stops_df, unassigned, zone_summary = \
                        run_routing(file_bytes, mode.lower())

                    st.session_state.update({
                        "excel_bytes":  excel_bytes,
                        "routes":       routes,
                        "stops_count":  len(stops_df),
                        "unassigned":   unassigned,
                        "zone_summary": zone_summary,
                        "mode":         mode,
                    })
                except SystemExit as e:
                    st.error(f"❌ {e}")

    if "excel_bytes" in st.session_state:
        vehs    = len(st.session_state["routes"])
        stops   = st.session_state["stops_count"]
        skipped = len(st.session_state.get("unassigned", []))

        c1, c2, c3 = st.columns(3)
        c1.metric("Vehicles Routed", vehs)
        c2.metric("Stops Assigned",  stops)
        c3.metric("Unassigned",       skipped,
                  delta=f"-{skipped}" if skipped else None,
                  delta_color="inverse")

        st.download_button(
            "⬇  Download routes_output.xlsx",
            st.session_state["excel_bytes"],
            file_name="routes_output.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )

        if skipped:
            st.warning(
                f"⚠️ **{skipped}** stop(s) could not be assigned. "
                "Check the **Unassigned** sheet in the downloaded file."
            )

        zs = st.session_state.get("zone_summary")
        if zs:
            st.markdown("---")
            st.subheader("Zone Load — After Rebalancing")
            df_zs = pd.DataFrame(zs)[
                ["vehicle", "zone", "orig_stops", "stops", "kg", "status"]
            ].copy()
            df_zs.columns = ["Vehicle", "Zone", "Original", "Final", "KG", "Status"]

            def _color(row):
                c = {
                    "OVERLOADED": "background-color:#FFDDC1",
                    "LIGHT":      "background-color:#FFFACD",
                    "NO ORDERS":  "background-color:#E8E8E8",
                }.get(row["Status"], "")
                return [c] * len(row)

            st.dataframe(
                df_zs.style.apply(_color, axis=1),
                use_container_width=True,
                hide_index=True,
                height=420,
            )


# ── Page: Admin ───────────────────────────────────────────────────────────────
elif page == "⚙️ Admin":
    st.header("Admin Panel")

    # Authentication - use secrets if available, fallback to default for local dev
    try:
        admin_pw = st.secrets["admin_password"]
    except Exception:
        admin_pw = "dsd2024"  # local dev fallback - override via Streamlit Cloud secrets

    if not st.session_state.get("admin_auth"):
        with st.form("login_form"):
            pw = st.text_input("Password", type="password",
                               placeholder="Enter admin password")
            if st.form_submit_button("Login", type="primary"):
                if pw == admin_pw:
                    st.session_state["admin_auth"] = True
                    st.rerun()
                else:
                    st.error("Incorrect password.")
        st.stop()

    col_status, col_logout = st.columns([5, 1])
    col_status.success("✅ Logged in as Admin")
    if col_logout.button("Logout"):
        st.session_state["admin_auth"] = False
        st.rerun()

    st.markdown("---")

    tab_master, tab_customer, tab_fleet, tab_cfg = st.tabs([
        "📋 Customer Master",
        "➕ New Customer",
        "🚛 Fleet & Zones",
        "⚙️ Configuration",
    ])

    with tab_master:
        st.subheader("Customer Master")
        mp = config.CUSTOMER_MASTER

        if os.path.exists(mp):
            mdf = pd.read_excel(mp, sheet_name="Customers")
            c1, c2, c3 = st.columns(3)
            c1.metric("Total Customers", len(mdf))
            c2.metric("Zones",           mdf["zone"].nunique())
            c3.metric("With History",    int((mdf["visits"] > 0).sum()))

            st.markdown("**Zone Distribution**")
            zd = (
                mdf.groupby("zone")
                .agg(
                    customers        =("customer_code", "count"),
                    dominant_vehicle =("preferred_vehicle",
                                       lambda x: x.value_counts().index[0]),
                    avg_kg           =("avg_weight_kg", "mean"),
                )
                .round(1)
                .reset_index()
            )
            st.dataframe(zd, use_container_width=True, hide_index=True, height=320)
        else:
            st.warning("customer_master.xlsx not found. Run _setup/run_setup.bat locally first.")

        st.markdown("---")
        cu, cd = st.columns(2)

        with cu:
            st.markdown("**Replace Customer Master**")
            new_m = st.file_uploader("Upload new customer_master.xlsx",
                                     type=["xlsx"], key="master_up")
            if new_m and st.button("✅ Replace Master", type="primary"):
                os.makedirs(os.path.dirname(mp), exist_ok=True)
                with open(mp, "wb") as f:
                    f.write(new_m.getvalue())
                st.success("Customer master replaced.")
                st.rerun()

        with cd:
            st.markdown("**Download Current Master**")
            if os.path.exists(mp):
                with open(mp, "rb") as f:
                    st.download_button("⬇ Download customer_master.xlsx",
                                       f.read(), "customer_master.xlsx",
                                       use_container_width=True)

    with tab_customer:
        st.subheader("Add New Customer")
        mp2 = config.CUSTOMER_MASTER

        if not os.path.exists(mp2):
            st.error("Customer master not found.")
        else:
            mdf2 = pd.read_excel(mp2, sheet_name="Customers")
            mdf2["customer_code"] = mdf2["customer_code"].astype(str)
            zones_list = sorted(mdf2["zone"].dropna().unique().tolist())

            with st.form("new_cust_form"):
                col1, col2 = st.columns(2)
                with col1:
                    code   = st.text_input("Customer Code *")
                    name   = st.text_input("Customer Name *")
                    street = st.text_input("Street / Address")
                    lat    = st.number_input("Latitude *",  value=42.005,
                                             format="%.6f", step=0.0001)
                    lon    = st.number_input("Longitude *", value=21.435,
                                             format="%.6f", step=0.0001)
                with col2:
                    zone     = st.selectbox("Zone *", zones_list)
                    pref     = st.selectbox("Preferred Vehicle",
                                            ["Kamion", "Furgon", "Van"])
                    eligible = st.multiselect("Eligible Vehicles *",
                                              ["Kamion", "Furgon", "Van"],
                                              default=[pref])
                    tw_s = st.text_input("Time Window Start", "06:00")
                    tw_e = st.text_input("Time Window End",   "18:00")

                if st.form_submit_button("Add Customer", type="primary"):
                    if not code or not name or not eligible:
                        st.error("Please fill all required fields (*).")
                    elif code in mdf2["customer_code"].values:
                        st.error(f"Code **{code}** already exists in the master.")
                    else:
                        wb_m  = openpyxl.load_workbook(mp2)
                        ws_m  = wb_m["Customers"]
                        cols  = [c.value for c in ws_m[1]]
                        nrow  = {
                            "customer_code":     code,
                            "customer_name":     name,
                            "street":            street,
                            "latitude":          lat,
                            "longitude":         lon,
                            "zone":              zone,
                            "special_zone":      "",
                            "eligible_vehicles": ",".join(eligible),
                            "preferred_vehicle": pref,
                            "time_window_start": tw_s,
                            "time_window_end":   tw_e,
                            "visits":            0,
                            "avg_weight_kg":     0,
                            "avg_cases":         0,
                            "kg_per_case":       0,
                            "vehicle_breakdown": "New customer - no history",
                            "notes":             "Added via web admin",
                        }
                        ws_m.append([nrow.get(c, "") for c in cols])
                        wb_m.save(mp2)
                        st.success(f"✅ **{name}** ({code}) added to zone **{zone}**.")

    with tab_fleet:
        st.subheader("Fleet & Zone Assignments")

        c1, c2, c3 = st.columns(3)
        c1.metric("Kamion", sum(1 for v in config.FLEET if v["type"] == "Kamion"))
        c2.metric("Furgon", sum(1 for v in config.FLEET if v["type"] == "Furgon"))
        c3.metric("Van",    sum(1 for v in config.FLEET if v["type"] == "Van"))

        st.markdown("---")
        st.subheader("Zone Suggestion")
        st.markdown(
            "Click **Generate** to create a `today.xlsx` with zone assignments "
            "pre-filled for all 44 vehicles. Download, review in Excel if needed, "
            "then upload it on the Route Planning page."
        )

        if st.button("⚙️ Generate today.xlsx with Zone Suggestions", type="primary"):
            ensure_today_xlsx()
            try:
                import contextlib
                _buf = io.StringIO()
                with contextlib.redirect_stdout(_buf):
                    sz.suggest_assignments()
                with open(config.TODAY_FILE, "rb") as f:
                    st.session_state["zone_today_bytes"] = f.read()
                st.success("✅ Zone suggestions generated - download below.")
            except SystemExit as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Error: {e}")

        if st.session_state.get("zone_today_bytes"):
            st.download_button(
                "⬇ Download today.xlsx (zones pre-filled)",
                st.session_state["zone_today_bytes"],
                file_name="today.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        st.markdown("---")

        if os.path.exists(config.TODAY_FILE):
            try:
                xl  = pd.ExcelFile(config.TODAY_FILE)
                if "Vehicles" in xl.sheet_names:
                    vdf = xl.parse("Vehicles").dropna(subset=["vehicle_name"])
                    vdf = vdf[vdf["vehicle_name"].astype(str).str.len() > 2]
                    vdf = vdf[["vehicle_name", "vehicle_type", "zone",
                                "available", "max_trips_per_day"]]
                    vdf.columns = ["Vehicle", "Type", "Zone", "Available", "Max Trips"]
                    st.markdown("**Current today.xlsx Vehicle Sheet**")
                    st.dataframe(vdf, use_container_width=True,
                                 hide_index=True, height=320)
            except Exception:
                pass

    with tab_cfg:
        st.subheader("Current Configuration")
        st.info(
            "Configuration is managed in **config.py**. "
            "Edit and push to GitHub - changes apply on the next app load."
        )

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Depot**")
            st.code(
                f"Latitude:    {config.DEPOT_LAT}\n"
                f"Longitude:   {config.DEPOT_LON}\n"
                f"Open:        {config.DEPOT_OPEN}\n"
                f"Close:       {config.DEPOT_CLOSE}"
            )
            st.markdown("**Trip Capacities**")
            for vtype, cap in config.TRIP_CAPACITY.items():
                st.text(f"  {vtype}: {cap:,} kg")

        with c2:
            st.markdown("**Routing**")
            st.code(
                f"Max stops/day:    {config.MAX_STOPS_PER_DAY}\n"
                f"Max trips (norm): {config.MAX_TRIPS_NORMAL}\n"
                f"Max trips (peak): {config.MAX_TRIPS_PEAK}\n"
                f"Driver hours:     {config.MAX_DRIVER_HOURS}\n"
                f"Solver time:      {config.SOLVER_TIME_LIMIT_SECONDS} s\n"
                f"Avg speed:        {config.AVERAGE_SPEED_KMH} km/h"
            )
            st.markdown("**Special Zones**")
            for zname, zdef in config.SPECIAL_ZONES.items():
                st.text(
                    f"  {zname}: {zdef['primary_vehicle']}  "
                    f"{zdef['time_window_start']}-{zdef['time_window_end']}"
                )

        st.markdown("---")
        st.markdown("**Territory Cluster Counts**")
        for vtype, n in config.N_CLUSTERS_TERRITORY_PER_TYPE.items():
            st.text(f"  {vtype}: {n} zones")
        st.text(f"  + {len(config.SPECIAL_ZONES)} special zones")
        total = sum(config.N_CLUSTERS_TERRITORY_PER_TYPE.values()) + len(config.SPECIAL_ZONES)
        st.text(f"  = {total} total zones (matches fleet of {len(config.FLEET)} vehicles)")
