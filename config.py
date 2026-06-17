# ─────────────────────────────────────────────────────────────────────────────
#  config.py  –  Master configuration. Edit once; do not change daily.
# ─────────────────────────────────────────────────────────────────────────────

# ── Depot ─────────────────────────────────────────────────────────────────────
DEPOT_LAT = 42.0050
DEPOT_LON  = 21.4000

DEPOT_OPEN  = "05:30"
DEPOT_CLOSE = "20:00"

# ── Current fleet definition ─────────────────────────────────────────────────
FLEET = [
    # Kamion (14)
    {"name": "KAMION_01",  "type": "Kamion"},
    {"name": "KAMION_02",  "type": "Kamion"},
    {"name": "KAMION_03",  "type": "Kamion"},
    {"name": "KAMION_04",  "type": "Kamion"},
    {"name": "KAMION_05",  "type": "Kamion"},
    {"name": "KAMION_06",  "type": "Kamion"},
    {"name": "KAMION_07",  "type": "Kamion"},
    {"name": "KAMION_08",  "type": "Kamion"},
    {"name": "KAMION_09",  "type": "Kamion"},
    {"name": "KAMION_10",  "type": "Kamion"},
    {"name": "KAMION_11",  "type": "Kamion"},
    {"name": "KAMION_12",  "type": "Kamion"},
    {"name": "KAMION_13",  "type": "Kamion"},
    {"name": "KAMION_14",  "type": "Kamion"},
    # Furgon (19)
    {"name": "FURGON_01",  "type": "Furgon"},
    {"name": "FURGON_02",  "type": "Furgon"},
    {"name": "FURGON_03",  "type": "Furgon"},
    {"name": "FURGON_04",  "type": "Furgon"},
    {"name": "FURGON_05",  "type": "Furgon"},
    {"name": "FURGON_06",  "type": "Furgon"},
    {"name": "FURGON_07",  "type": "Furgon"},
    {"name": "FURGON_08",  "type": "Furgon"},
    {"name": "FURGON_09",  "type": "Furgon"},
    {"name": "FURGON_10",  "type": "Furgon"},
    {"name": "FURGON_11",  "type": "Furgon"},
    {"name": "FURGON_12",  "type": "Furgon"},
    {"name": "FURGON_13",  "type": "Furgon"},
    {"name": "FURGON_14",  "type": "Furgon"},
    {"name": "FURGON_15",  "type": "Furgon"},
    {"name": "FURGON_16",  "type": "Furgon"},
    {"name": "FURGON_17",  "type": "Furgon"},
    {"name": "FURGON_18",  "type": "Furgon"},
    {"name": "FURGON_19",  "type": "Furgon"},
    # Van (11)
    {"name": "VAN_01",     "type": "Van"},
    {"name": "VAN_02",     "type": "Van"},
    {"name": "VAN_03",     "type": "Van"},
    {"name": "VAN_04",     "type": "Van"},
    {"name": "VAN_05",     "type": "Van"},
    {"name": "VAN_06",     "type": "Van"},
    {"name": "VAN_07",     "type": "Van"},
    {"name": "VAN_08",     "type": "Van"},
    {"name": "VAN_09",     "type": "Van"},
    {"name": "VAN_10",     "type": "Van"},
    {"name": "VAN_11",     "type": "Van"},
]

# ── Trip capacities (kg per single loaded trip) ────────────────────────────────
TRIP_CAPACITY = {
    "Kamion": 6_000,
    "Furgon": 5_200,
    "Van":    3_200,
}

# ── Multi-trip limits ─────────────────────────────────────────────────────────
MAX_TRIPS_NORMAL = 2
MAX_TRIPS_PEAK   = 3
MAX_DRIVER_HOURS = 10

# ── Routing parameters ────────────────────────────────────────────────────────
AVERAGE_SPEED_KMH  = 40
ROAD_FACTOR        = 1.3
AVG_SERVICE_MIN    = 12
SOLVER_TIME_LIMIT_SECONDS = 120

# ── Customer eligibility thresholds ──────────────────────────────────────────
NEW_CUSTOMER_DEFAULT_VEHICLES = ["Kamion", "Van"]
FURGON_ELIGIBLE_FROM_KAMION_PCT = 10
KAMION_ELIGIBLE_FROM_VAN_PCT    = 15
FURGON_ELIGIBLE_FROM_VAN_PCT    = 15

# ── Stop count cap ────────────────────────────────────────────────────────────
MAX_STOPS_PER_DAY = 12

# ── Geo clustering ────────────────────────────────────────────────────────────
# One territory zone per vehicle (14 Kamion + 19 Furgon + 11 Van = 44 vehicles).
# Plostad occupies 1 Kamion slot, Carsija occupies 1 Van slot, so the dynamic
# clusters total 42 (13 + 19 + 10) + 2 special = 44 zones = 44 vehicles.
N_CLUSTERS_TERRITORY_PER_TYPE = {
    "Kamion": 13,   # 14 total minus 1 for Plostad
    "Furgon": 19,
    "Van":    10,   # 11 total minus 1 for Carsija
}

# ── Special zones (manually defined, override clustering) ─────────────────────
SPECIAL_ZONES = {
    "Plostad": {
        "polygon": [
            (41.998885867010074, 21.43007861785311),
            (41.99743472470818,  21.428190342779853),
            (41.9952818702249,   21.427997223738274),
            (41.99317678647601,  21.428125969785956),
            (41.99204447781636,  21.431687943219593),
            (41.993926331832014, 21.43636571556016),
            (41.994994817398236, 21.43700944569877),
            (41.996605485618865, 21.43258916541365),
            (41.998885867010074, 21.43007861785311),
        ],
        "allowed_vehicles":  ["Kamion", "Van"],
        "time_window_start": "06:00",
        "time_window_end":   "18:00",
        "primary_vehicle":   "Kamion",
    },
    "Carsija": {
        "polygon": [
            (41.99938020467104, 21.431494824310327),
            (41.99899749200797, 21.439927689126115),
            (42.00443498522089, 21.439477078025767),
            (42.00599431287442, 21.435380808411548),
            (42.00582519403957, 21.4332414344641),
            (41.99938020467104, 21.431494824310327),
        ],
        "allowed_vehicles":  ["Van"],
        "time_window_start": "06:00",
        "time_window_end":   "09:00",
        "primary_vehicle":   "Van",
    },
}

# ── Files ─────────────────────────────────────────────────────────────────────
HISTORICAL_FILE = "_setup/Za model.xlsx"
CUSTOMER_MASTER = "_setup/customer_master.xlsx"
TODAY_FILE      = "today.xlsx"
OUTPUT_FILE     = "routes_output.xlsx"
