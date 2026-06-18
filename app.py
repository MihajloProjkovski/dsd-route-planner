"""
app.py - DSD Route Planner Web Application
"""

import io
import os
import sys
from datetime import date

import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config
import route_planner as rp

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
  div[data-testid="metric-container"] {
    background: #f8f9fa; border-radius: 8px; padding: 8px;
  }
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
st.sidebar.caption("v2.0 · Skopje DSD")


def make_blank_template() -> bytes:
    HDR_FILL = PatternFill("solid", fgColor="1F4E79")
    HDR_FONT = Font(color="FFFFFF", bold=True)
    wb = openpyxl.Workbook()
    ws_o = wb.active
    ws_o.title = "Orders"
    ws_o.append(["customer_code", "customer_name", "cases", "kg"])
    for cell in ws_o[1]:
        cell.fill = HDR_FILL; cell.font = HDR_FONT
        cell.alignment = Alignment(horizontal="center")
    for col, w in zip(["A","B","C","D"], [18, 35, 10, 12]):
        ws_o.column_dimensions[col].width = w
    ws_o.freeze_panes = "A2"

    ws_v = wb.create_sheet("Vehicles")
    headers = ["vehicle_name", "vehicle_type", "capacity_kg",
               "zone", "available", "max_trips_per_day", "notes"]
    ws_v.append(headers)
    for cell in ws_v[1]:
        cell.fill = HDR_FILL; cell.font = HDR_FONT
        cell.alignment = Alignment(horizontal="center")
    for col_letter, w in zip(["A","B","C","D","E","F","G"], [20,12,14,22,12,18,25]):
        ws_v.column_dimensions[col_letter].width = w
    ws_v.append([])
    ws_v.append(["HOW TO FILL:"])
    ws_v.append(["vehicle_name    -> Your vehicle name/plate (e.g. DSD 1, F 3, Van 7)"])
    ws_v.append(["vehicle_type    -> Kamion | Furgon | Van"])
    ws_v.append(["capacity_kg     -> Usable kg per trip (e.g. 6000, 5200, 3200)"])
    ws_v.append(["zone            -> Same as vehicle_name for dedicated zone. Float for overflow."])
    ws_v.append(["available       -> TRUE = working today, FALSE = unavailable"])
    ws_v.append(["max_trips       -> 2 = normal day, 3 = heavy day"])
    ws_v.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


def run_routing(file_bytes: bytes, mode: str):
    with open(config.TODAY_FILE, "wb") as f:
        f.write(file_bytes)
    stops_df    = rp.load_orders(config.TODAY_FILE, config.CUSTOMER_MASTER)
    vehicles_df = rp.load_vehicles(config.TODAY_FILE)
    zone_affinity = (mode == "smart")
    routes, unassigned, stops_df = rp.solve(stops_df, vehicles_df, zone_affinity=zone_affinity)
    zone_summary = None
    if zone_affinity:
        vehicle_zones = {}
        for _, row in vehicles_df.iterrows():
            z = str(row["zone"]).strip()
            if z.lower() not in ("", "nan", "float", "none"):
                vehicle_zones[row["vehicle_name"]] = z
        original_counts = {}
        for _, row in stops_df.iterrows():
            sz = str(row.get("zone", "")).strip()
            for vname, vzone in vehicle_zones.items():
                if vzone == sz:
                    original_counts[vname] = original_counts.get(vname, 0) + 1
                    break
        zone_summary = rp.build_zone_summary_from_routes(routes, original_counts)
    excel_buf = io.BytesIO()
    rp.write_excel(routes, stops_df, unassigned, excel_buf, zone_summary)
    excel_buf.seek(0)
    map_html = rp.generate_route_map(routes) if routes else None
    return excel_buf.getvalue(), routes, stops_df, unassigned, zone_summary, map_html


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


if page == "🗺 Route Planning":
    st.header("Route Planning")
    col_up, col_mode = st.columns([3, 1])
    with col_up:
        uploaded = st.file_uploader(
            "Upload **today.xlsx** (Orders + Vehicles sheets)",
            type=["xlsx"],
            help="Need a blank template? Admin → Fleet & Zones → Download blank template.",
        )
    with col_mode:
        st.markdown("**Mode**")
        mode_label = st.radio(
            "mode", ["🧠 SMART", "🔓 FREE"], label_visibility="collapsed",
            help=(
                "**SMART** — Zone-aware. Each vehicle biased to its own customers "
                "but solver can reassign for efficiency. Daily use.\n\n"
                "**FREE** — No zone bias. Pure distance. Use as benchmark or backup."
            ),
        )
        mode = "smart" if "SMART" in mode_label else "free"

    if uploaded:
        file_bytes = uploaded.getvalue()
        try:
            prev = pd.read_excel(io.BytesIO(file_bytes), sheet_name="Orders").dropna(how="all")
            st.caption(f"📦 **{len(prev)}** orders detected")
        except Exception:
            pass
        if mode == "smart" and not check_zones_assigned(file_bytes):
            st.warning("⚠️ No zone assignments found in Vehicles sheet. "
                       "Switch to FREE mode or set zones in the Vehicles sheet.")

        if st.button("▶  Generate Routes", type="primary"):
            with st.spinner(f"Running {'SMART' if mode=='smart' else 'FREE'} routing "
                            f"(up to {config.SOLVER_TIME_LIMIT_SECONDS//60} min)…"):
                try:
                    excel_bytes, routes, stops_df, unassigned, zone_summary, map_html = \
                        run_routing(file_bytes, mode)
                    st.session_state.update({
                        "excel_bytes": excel_bytes, "routes": routes,
                        "stops_count": len(stops_df), "unassigned": unassigned,
                        "zone_summary": zone_summary, "map_html": map_html,
                    })
                except SystemExit as e:
                    st.error(f"❌ {e}")

    if "excel_bytes" in st.session_state:
        c1, c2, c3 = st.columns(3)
        c1.metric("Vehicles Routed", len(st.session_state["routes"]))
        c2.metric("Stops Assigned",  st.session_state["stops_count"])
        skipped = len(st.session_state.get("unassigned", []))
        c3.metric("Unassigned", skipped,
                  delta=f"-{skipped}" if skipped else None, delta_color="inverse")

        d1, d2 = st.columns(2)
        with d1:
            st.download_button("⬇  Download routes_output.xlsx",
                               st.session_state["excel_bytes"],
                               file_name="routes_output.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               type="primary", use_container_width=True)
        with d2:
            if st.session_state.get("map_html"):
                st.download_button("🗺  Download Route Map (HTML)",
                                   st.session_state["map_html"].encode("utf-8"),
                                   file_name="route_map.html", mime="text/html",
                                   use_container_width=True)

        if skipped:
            st.warning(f"⚠️ **{skipped}** stop(s) unassigned — see Unassigned sheet.")

        if st.session_state.get("map_html"):
            st.markdown("---")
            st.subheader("Interactive Route Map")
            st.caption("Filter by vehicle type, vehicle name, or trip number in the map sidebar.")
            components.html(st.session_state["map_html"], height=540, scrolling=False)

        zs = st.session_state.get("zone_summary")
        if zs:
            st.markdown("---")
            st.subheader("Zone Load Summary")
            df_zs = pd.DataFrame(zs)[["vehicle","zone","orig_stops","stops","kg","status"]].copy()
            df_zs.columns = ["Vehicle","Zone","Original","Final","KG","Status"]
            def _color(row):
                c = {"OVERLOADED":"background-color:#FFDDC1","LIGHT":"background-color:#FFFACD",
                     "NO ORDERS":"background-color:#E8E8E8"}.get(row["Status"],"")
                return [c]*len(row)
            st.dataframe(df_zs.style.apply(_color, axis=1),
                         use_container_width=True, hide_index=True, height=380)


elif page == "⚙️ Admin":
    st.header("Admin Panel")
    try:
        admin_pw = st.secrets["admin_password"]
    except Exception:
        admin_pw = "dsd2024"

    if not st.session_state.get("admin_auth"):
        with st.form("login_form"):
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Login", type="primary"):
                if pw == admin_pw:
                    st.session_state["admin_auth"] = True
                    st.rerun()
                else:
                    st.error("Incorrect password.")
        st.stop()

    col_s, col_l = st.columns([5, 1])
    col_s.success("✅ Logged in as Admin")
    if col_l.button("Logout"):
        st.session_state["admin_auth"] = False
        st.rerun()
    st.markdown("---")

    tab_master, tab_customer, tab_fleet, tab_cfg = st.tabs([
        "📋 Customer Master", "➕ New Customer", "🚛 Fleet & Zones", "⚙️ Configuration"])

    with tab_master:
        st.subheader("Customer Master")
        mp = config.CUSTOMER_MASTER
        if os.path.exists(mp):
            mdf = pd.read_excel(mp, sheet_name="Customers")
            c1,c2,c3 = st.columns(3)
            c1.metric("Customers", len(mdf))
            c2.metric("Zones", mdf["zone"].nunique())
            c3.metric("With History", int((mdf["visits"]>0).sum()))
            zd = (mdf.groupby("zone").agg(
                customers=("customer_code","count"),
                dominant=("preferred_vehicle", lambda x: x.value_counts().index[0]),
                avg_kg=("avg_weight_kg","mean")).round(1).reset_index())
            st.dataframe(zd, use_container_width=True, hide_index=True, height=300)
        else:
            st.warning("customer_master.xlsx not found.")
        st.markdown("---")
        cu, cd = st.columns(2)
        with cu:
            st.markdown("**Upload New Master**")
            new_m = st.file_uploader("Upload customer_master.xlsx", type=["xlsx"], key="master_up")
            if new_m and st.button("✅ Replace", type="primary"):
                os.makedirs(os.path.dirname(mp), exist_ok=True)
                with open(mp, "wb") as f: f.write(new_m.getvalue())
                st.success("Customer master replaced.")
                st.rerun()
        with cd:
            st.markdown("**Download Current Master**")
            if os.path.exists(mp):
                with open(mp, "rb") as f:
                    st.download_button("⬇ Download", f.read(), "customer_master.xlsx",
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
                    code=st.text_input("Customer Code *"); name=st.text_input("Customer Name *")
                    street=st.text_input("Street")
                    lat=st.number_input("Latitude *", value=42.005, format="%.6f", step=0.0001)
                    lon=st.number_input("Longitude *", value=21.435, format="%.6f", step=0.0001)
                with col2:
                    zone=st.selectbox("Zone (= vehicle name) *", zones_list)
                    pref=st.selectbox("Preferred Vehicle", ["Kamion","Furgon","Van"])
                    eligible=st.multiselect("Eligible Vehicles *", ["Kamion","Furgon","Van"], default=[pref])
                    tw_s=st.text_input("TW Start","06:00"); tw_e=st.text_input("TW End","18:00")
                if st.form_submit_button("Add Customer", type="primary"):
                    if not code or not name or not eligible:
                        st.error("Fill all required fields.")
                    elif code in mdf2["customer_code"].values:
                        st.error(f"Code {code} already exists.")
                    else:
                        wb_m=openpyxl.load_workbook(mp2); ws_m=wb_m["Customers"]
                        cols=[c.value for c in ws_m[1]]
                        nrow={"customer_code":code,"customer_name":name,"street":street,
                              "latitude":lat,"longitude":lon,"zone":zone,"special_zone":"",
                              "eligible_vehicles":",".join(eligible),"preferred_vehicle":pref,
                              "time_window_start":tw_s,"time_window_end":tw_e,
                              "visits":0,"avg_weight_kg":0,"avg_cases":0,"kg_per_case":0,
                              "vehicle_breakdown":"New customer","notes":"Added via web admin"}
                        ws_m.append([nrow.get(c,"") for c in cols])
                        wb_m.save(mp2)
                        st.success(f"✅ {name} ({code}) added to zone {zone}.")

    with tab_fleet:
        st.subheader("Fleet & Zones")
        st.markdown("#### Blank today.xlsx Template")
        st.markdown("Download, fill in your vehicles once, then use daily.")
        st.download_button("⬇  Download blank today.xlsx",
                           make_blank_template(), file_name="today.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           type="primary")
        st.markdown("---")
        st.markdown("""**Vehicles sheet columns:**

| Column | Description |
|---|---|
| `vehicle_name` | Name/plate — e.g. `DSD 1`, `F 3`, `Van 7` |
| `vehicle_type` | `Kamion`, `Furgon`, or `Van` |
| `capacity_kg` | Usable load per trip in kg |
| `zone` | Same as `vehicle_name` for dedicated zone. `Float` for overflow. |
| `available` | `TRUE` = working today |
| `max_trips_per_day` | `2` normal, `3` heavy |
""")
        if os.path.exists(config.CUSTOMER_MASTER):
            st.markdown("**Zone Summary from Customer Master**")
            mdf3 = pd.read_excel(config.CUSTOMER_MASTER, sheet_name="Zone Summary")
            st.dataframe(mdf3, use_container_width=True, hide_index=True, height=260)

    with tab_cfg:
        st.subheader("Configuration")
        st.info("Edit **config.py** and push to GitHub to apply changes.")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Depot**")
            st.code(f"Lat: {config.DEPOT_LAT}  Lon: {config.DEPOT_LON}\n"
                    f"Open: {config.DEPOT_OPEN}  Close: {config.DEPOT_CLOSE}")
            st.markdown("**Capacity Defaults**")
            for vt, cap in config.TRIP_CAPACITY.items():
                st.text(f"  {vt}: {cap:,} kg/trip")
        with c2:
            st.markdown("**Solver**")
            st.code(f"Max stops/day : {config.MAX_STOPS_PER_DAY}\n"
                    f"Solver time   : {config.SOLVER_TIME_LIMIT_SECONDS} s\n"
                    f"Zone penalty  : {config.ZONE_AFFINITY_PENALTY_KM} km\n"
                    f"Avg speed     : {config.AVERAGE_SPEED_KMH} km/h")
            st.markdown("**Special Zones**")
            for zn, zd in config.SPECIAL_ZONES.items():
                st.text(f"  {zn}: {zd['primary_vehicle']}  "
                        f"{zd['time_window_start']}–{zd['time_window_end']}")
