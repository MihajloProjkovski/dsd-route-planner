#!/usr/bin/env python3
"""
suggest_zones.py
----------------
Reads the Zone Summary from _setup/customer_master.xlsx and suggests a
zone assignment for every vehicle in today.xlsx, then writes it back.

Run:  python suggest_zones.py
  or  double-click suggest_zones.bat
"""

import sys
import os
import config
import pandas as pd
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter


TYPE_FILLS = {
    "Kamion": PatternFill("solid", fgColor="D6E4F7"),
    "Furgon": PatternFill("solid", fgColor="D6F7E4"),
    "Van":    PatternFill("solid", fgColor="FFF3CD"),
}
HDR_FILL = PatternFill("solid", fgColor="1F4E79")
HDR_FONT = Font(color="FFFFFF", bold=True)
FLOAT_FILL = PatternFill("solid", fgColor="F0F0F0")


def suggest_assignments():
    master = config.CUSTOMER_MASTER
    today  = config.TODAY_FILE

    if not os.path.exists(master):
        sys.exit(f"ERROR: {master} not found. Run _setup/run_setup.bat first.")
    if not os.path.exists(today):
        sys.exit(f"ERROR: {today} not found. Run _setup/run_setup.bat first.")

    # Load zone summary
    zs = pd.read_excel(master, sheet_name="Zone Summary")
    zs.columns = zs.columns.str.strip().str.lower().str.replace(" ", "_")
    zs = zs.dropna(subset=["zone"]).copy()

    # Build per-type zone queues, heaviest first
    special_primary = {
        name: zdef["primary_vehicle"]
        for name, zdef in config.SPECIAL_ZONES.items()
    }

    special_rows = zs[zs["zone"].isin(special_primary.keys())].copy()
    dynamic_rows = zs[~zs["zone"].isin(special_primary.keys())].copy()
    dynamic_rows = dynamic_rows.sort_values("avg_weight", ascending=False)

    queues = {"Kamion": [], "Furgon": [], "Van": []}

    for _, row in special_rows.iterrows():
        vtype = special_primary[row["zone"]]
        if vtype in queues:
            queues[vtype].insert(0, row["zone"])

    for _, row in dynamic_rows.iterrows():
        vtype = row["dominant_veh"]
        if vtype in queues:
            queues[vtype].append(row["zone"])

    vehicles_by_type = {"Kamion": [], "Furgon": [], "Van": []}
    for v in config.FLEET:
        if v["type"] in vehicles_by_type:
            vehicles_by_type[v["type"]].append(v["name"])

    assignments = {}
    secondary   = {}
    remaining_queues = {k: list(v) for k, v in queues.items()}

    for vtype, veh_names in vehicles_by_type.items():
        zone_queue = remaining_queues.get(vtype, [])
        for vname in veh_names:
            if zone_queue:
                assignments[vname] = zone_queue.pop(0)
            else:
                assignments[vname] = "Float"

    unassigned_zones = [z for zq in remaining_queues.values() for z in zq]
    for leftover_zone in unassigned_zones:
        vtype = leftover_zone.split("_")[0] if "_" in leftover_zone else "Kamion"
        if vtype not in vehicles_by_type:
            vtype = "Kamion"
        float_veh = next(
            (v for v in reversed(vehicles_by_type[vtype])
             if assignments.get(v) == "Float"),
            None
        )
        if float_veh:
            secondary[float_veh] = leftover_zone

    print("=" * 55)
    print("  Zone Assignment Suggestion")
    print("=" * 55)

    counts = {"Kamion": 0, "Furgon": 0, "Van": 0}
    for vtype, veh_names in vehicles_by_type.items():
        print(f"\n  {vtype} ({len(veh_names)} vehicles):")
        for vname in veh_names:
            zone = assignments[vname]
            sec  = secondary.get(vname, "")
            if zone == "Float":
                tag = "  [overflow]"
            elif sec:
                tag = f"  + covers {sec} (secondary)"
            else:
                tag = ""
            print(f"    {vname:<22} -> {zone}{tag}")
            if zone != "Float":
                counts[vtype] += 1

    print(f"\n  Zones assigned : {sum(counts.values())}")
    print(f"  Float vehicles : {sum(1 for z in assignments.values() if z == 'Float')}")

    wb = openpyxl.load_workbook(today)

    if "Vehicles" not in wb.sheetnames:
        sys.exit("ERROR: today.xlsx is missing the 'Vehicles' sheet.")

    ws = wb["Vehicles"]
    header = {cell.value: cell.column for cell in ws[1] if cell.value}
    name_col = header.get("vehicle_name")
    zone_col = header.get("zone")

    if not name_col or not zone_col:
        sys.exit("ERROR: Vehicles sheet must have 'vehicle_name' and 'zone' columns.")

    updated = 0
    for row in ws.iter_rows(min_row=2):
        vname_cell = row[name_col - 1]
        zone_cell  = row[zone_col  - 1]
        vname = str(vname_cell.value).strip() if vname_cell.value else ""
        if not vname or vname == "None" or vname.startswith("HOW TO"):
            continue
        if vname in assignments:
            primary = assignments[vname]
            sec     = secondary.get(vname, "")
            zone_cell.value = f"{primary},{sec}" if sec else primary
            fill = FLOAT_FILL if primary == "Float" else TYPE_FILLS.get(
                next((v["type"] for v in config.FLEET if v["name"] == vname), ""), None
            )
            if fill:
                for cell in row:
                    cell.fill = fill
            updated += 1

    try:
        wb.save(today)
    except PermissionError:
        print(f"\n  ERROR: Cannot save '{today}' - the file is open in Excel.")
        print(f"  Close today.xlsx and run suggest_zones.bat again.\n")
        sys.exit(1)

    print(f"\n  Saved to '{today}' - {updated} vehicles updated.")
    print("\n  You can manually override any zone in today.xlsx Vehicles sheet.")
    print("  Use 'Float' for vehicles you want to serve any zone as overflow.\n")


if __name__ == "__main__":
    suggest_assignments()
