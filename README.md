# DSD Route Planner

Streamlit application for creating freight zones from historical deliveries and
generating daily delivery routes.

## Start the application

1. Install Python 3.9 or newer.
2. Run `_setup\run_setup.bat` once to install the project dependencies and open
   the application.
3. On later days, start the application with `run.bat`.

The first launch opens the local Streamlit interface in a browser.

## Create freight zones

Open **Zone Builder** from the main navigation and download the input workbook template. Fill
these two sheets:

### `DeliveryHistory` — one row per order line

Required columns:

| Column | Meaning |
| --- | --- |
| `delivery_date` | Date of delivery |
| `shipment_id` | Unique ID for one shipment/trip on that date |
| `customer_id` | Stable customer ID |
| `vehicle_id` | Name or ID of the vehicle used |
| `vehicle_type` | Vehicle type that delivered the shipment |
| `quantity_kg` | Weight on the order line |

Optional columns are `cases` and `delivery_status`. Cancelled,
failed, returned, and undelivered rows are excluded when a status is supplied.
Use a distinct shipment ID for each trip. Multiple order lines for the same
customer in one shipment are combined into one stop; the same customer on two
shipments that day counts as two stops.

### `Customers` — one row per customer

Required columns are `customer_id`, `latitude`, and `longitude`. Optional
columns are `customer_name`, `address`, `allowed_vehicle_types`, and
`service_minutes`. `allowed_vehicle_types` is for known hard restrictions;
separate historical vehicle types are retained as evidence, not inferred as
forbidden types.

Upload the workbook, set the target zone count (default 32), and create a
proposal. Review the zone workload table and map before choosing **Approve and
activate these zones**. Approval writes the output to
`_setup\customer_master.xlsx`, which is used by route planning. You can also
download the approved workbook.

The proposal ranks each customer's primary historical vehicle type using both
stop and kg shares, then clusters customers geographically with peak
stop/weight and observed weekday/month profiles considered during local workload
balancing. Zone vehicle-type
percentages and confidence are descriptive recommendations. Special zones
defined in `config.py` remain reserved. Results are deterministic for identical
inputs and settings.

History only supports conclusions about the dates and months present in the
file. For example, May–October data cannot establish January–February
workloads; the application warns about unobserved seasonal coverage.

## Generate daily routes

Maintain the fleet once in **Admin → Fleet Registry**. Download a daily
`today.xlsx` workbook from the Fleet Registry page; the Vehicles sheet is
pre-filled from the registry. Add the day's orders and mark unavailable
vehicles `FALSE`, then upload the workbook on **Route Planning** and choose
**Generate Routes**. Vehicles are matched dynamically to compatible zones;
vehicle-to-zone ownership is not required.

Route output includes an Excel plan, an interactive map, and warnings for any
unassigned stops.
