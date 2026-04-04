"""
Seed Data Transformation Script

Transforms historical baby tracking CSV data from the seeds/ folder
into the raw layer schema format, outputting to transformed_data/.

Supports multiple babies — auto-processes all entries in BABIES config.
Adding a new baby: add an entry to the BABIES list at the top of this file
and drop their CSV exports into baby_data/seeds/ as {Name}_diaper.csv etc.

Usage:
    python transform_seeds.py                  # all babies
    python transform_seeds.py --baby Imogen    # single baby
    python transform_seeds.py --list           # list configured babies
"""

import argparse
import csv
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, computed_field

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
SEEDS_DIR = SCRIPT_DIR.parent / "seeds"
OUTPUT_DIR = SCRIPT_DIR.parent / "transformed_data"

# Fixed project-specific UUID namespace — do not change this after first run
# or all baby IDs will change and break the database.
_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


# ---------------------------------------------------------------------------
# Baby config
# ---------------------------------------------------------------------------

class BabyConfig(BaseModel):
    """Configuration for a single baby. Add new babies here."""
    name: str
    date_of_birth: str          # YYYY-MM-DD
    gender: str                 # 'female' | 'male' | 'other'
    timezone: str               # IANA timezone, e.g. 'Australia/Sydney'
    # strptime format for the 'Time' column in this baby's CSV exports.
    # Ember exports: M/D/YY H:MM AM/PM  →  "%m/%d/%y %I:%M %p"
    # Imogen exports: D/M/YY h:mm am/pm →  "%d/%m/%y %I:%M %p"
    datetime_format: str = "%m/%d/%y %I:%M %p"
    birth_weight: str = ""
    birth_length: str = ""
    birth_head_circumference: str = ""
    notes: str = "Migrated from seed data"

    @computed_field  # type: ignore[misc]
    @property
    def baby_id(self) -> str:
        """Stable, deterministic UUID derived from the baby's name.

        Uses a fixed project namespace so the same name always produces the
        same UUID — re-running never breaks foreign key relationships.
        """
        return str(uuid.uuid5(_NAMESPACE, self.name))

    @property
    def created_at(self) -> str:
        return f"{self.date_of_birth}T00:00:00"


# ---------------------------------------------------------------------------
# Baby registry — add new babies here
# ---------------------------------------------------------------------------

BABIES: list[BabyConfig] = [
    BabyConfig(
        name="Ember",
        date_of_birth="2023-08-18",
        gender="female",
        timezone="Australia/Sydney",
        datetime_format="%d/%m/%y %I:%M %p",  # seeds use Australian day-first format
    ),
    BabyConfig(
        name="Imogen",
        date_of_birth="2026-03-14",  # inferred: earliest tracked entry is 14/3/26
        gender="female",
        timezone="Australia/Sydney",
        datetime_format="%d/%m/%y %I:%M %p",  # app exports day-first format
    ),
]


# ---------------------------------------------------------------------------
# CSV field lists (define once, use for reading and writing)
# ---------------------------------------------------------------------------

PROFILE_FIELDS = [
    "id", "name", "date_of_birth", "birth_weight", "birth_length",
    "birth_head_circumference", "gender", "timezone", "notes",
    "created_at", "updated_at", "is_active",
]
DIAPER_FIELDS = [
    "id", "baby_id", "timestamp", "has_urine", "urine_volume",
    "has_stool", "stool_consistency", "stool_color", "diaper_type",
    "notes", "created_at", "updated_at",
]
SLEEP_FIELDS = [
    "id", "baby_id", "start_time", "end_time", "sleep_type",
    "location", "sleep_quality", "sleep_environment", "wake_reason",
    "notes", "created_at", "updated_at",
]
FEEDING_FIELDS = [
    "id", "baby_id", "start_time", "end_time", "feeding_type",
    "breast_started", "left_breast_duration", "right_breast_duration",
    "volume_offered_ml", "volume_consumed_ml", "formula_type",
    "food_items", "appetite", "notes", "created_at", "updated_at",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_datetime(date_str: str, fmt: str) -> datetime:
    """Parse a date string using the given strptime format.

    Normalises the string before parsing:
    - strips surrounding whitespace
    - replaces narrow no-break space (U+202F) with a regular space
    - uppercases so 'am'/'pm' → 'AM'/'PM', making %p reliable across locales
    """
    normalised = date_str.strip().replace("\u202f", " ").upper()
    return datetime.strptime(normalised, fmt)


def format_timestamp(dt: datetime) -> str:
    """Format datetime to ISO-style timestamp string."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_int(value: str) -> str:
    """Parse an integer value, stripping commas and whitespace. Returns '' if empty."""
    if not value or not value.strip():
        return ""
    return str(int(value.replace(",", "").strip()))


def parse_float(value: str) -> str:
    """Parse a float value, stripping commas and whitespace. Returns '' if empty."""
    if not value or not value.strip():
        return ""
    return str(float(value.replace(",", "").strip()))


def read_csv_if_exists(path: Path) -> list[dict] | None:
    """Read a CSV file and return its rows as a list of dicts.

    Returns None (and prints a skip notice) if the file doesn't exist.
    """
    if not path.exists():
        print(f"    Skipping (not found): {path.name}")
        return None
    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    """Write rows to a CSV file, creating the file fresh each time."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def infer_sleep_type(start_time: datetime, duration_minutes: int) -> str:
    """Infer sleep type from start time and duration.

    NIGHTTIME: sleep starts between 7 PM–7 AM AND lasts more than 3 hours.
    NAP: everything else.
    """
    is_night_hours = start_time.hour >= 19 or start_time.hour < 7
    is_long_sleep = duration_minutes > 180
    return "NIGHTTIME" if (is_night_hours and is_long_sleep) else "NAP"


# ---------------------------------------------------------------------------
# Transform functions — each returns a list of row dicts for that baby
# ---------------------------------------------------------------------------

def transform_diaper_events(baby: BabyConfig, baby_id: str) -> list[dict]:
    """Transform {Name}_diaper.csv → diaper_events rows."""
    path = SEEDS_DIR / f"{baby.name}_diaper.csv"
    raw = read_csv_if_exists(path)
    if not raw:
        return []

    rows = []
    for row in raw:
        ts = parse_datetime(row["Time"], baby.datetime_format)
        status = row["Status"].strip()
        has_urine = status in ("Wet", "Mixed")
        has_stool = status in ("Dirty", "Mixed")
        rows.append({
            "id": str(uuid.uuid4()),
            "baby_id": baby_id,
            "timestamp": format_timestamp(ts),
            "has_urine": str(has_urine).lower(),
            "urine_volume": "MODERATE" if has_urine else "NONE",
            "has_stool": str(has_stool).lower(),
            "stool_consistency": "SOFT" if has_stool else "",
            "stool_color": "YELLOW" if has_stool else "",
            "diaper_type": "DISPOSABLE",
            "notes": row.get("Note", "").strip(),
            "created_at": format_timestamp(ts),
            "updated_at": format_timestamp(ts),
        })

    print(f"    Diaper events:     {len(rows):>5}")
    return rows


def transform_sleep_sessions(baby: BabyConfig, baby_id: str) -> list[dict]:
    """Transform {Name}_sleep.csv → sleep_sessions rows."""
    path = SEEDS_DIR / f"{baby.name}_sleep.csv"
    raw = read_csv_if_exists(path)
    if not raw:
        return []

    rows = []
    for row in raw:
        duration_str = row.get("Duration (min)", "").strip()
        if not duration_str:
            continue

        start_time = parse_datetime(row["Time"], baby.datetime_format)
        duration_minutes = int(duration_str)
        end_time = start_time + timedelta(minutes=duration_minutes)

        rows.append({
            "id": str(uuid.uuid4()),
            "baby_id": baby_id,
            "start_time": format_timestamp(start_time),
            "end_time": format_timestamp(end_time),
            "sleep_type": infer_sleep_type(start_time, duration_minutes),
            "location": "CRIB",
            "sleep_quality": "",
            "sleep_environment": "",
            "wake_reason": "NATURAL",
            "notes": row.get("Note", "").strip(),
            "created_at": format_timestamp(start_time),
            "updated_at": format_timestamp(start_time),
        })

    print(f"    Sleep sessions:    {len(rows):>5}")
    return rows


def transform_feeding_sessions(baby: BabyConfig, baby_id: str) -> list[dict]:
    """Transform nursing + pump + expressed CSVs → feeding_sessions rows.

    - Nursing   → feeding_type: BREAST
    - Pump      → feeding_type: BOTTLE (pumped milk fed to baby)
    - Expressed → feeding_type: BOTTLE (expressed milk fed to baby)
    """
    rows: list[dict] = []

    # 1. Nursing sessions
    nursing_raw = read_csv_if_exists(SEEDS_DIR / f"{baby.name}_nursing.csv")
    if nursing_raw:
        for row in nursing_raw:
            start_time = parse_datetime(row["Time"], baby.datetime_format)
            total_str = row.get("Total (min)", "").strip()
            duration_minutes = int(total_str) if total_str else 0
            end_time = start_time + timedelta(minutes=duration_minutes)
            start_side = row.get("Start side", "").strip()
            breast_started = start_side.upper() if start_side in ("Left", "Right") else ""
            rows.append({
                "id": str(uuid.uuid4()),
                "baby_id": baby_id,
                "start_time": format_timestamp(start_time),
                "end_time": format_timestamp(end_time),
                "feeding_type": "BREAST",
                "breast_started": breast_started,
                "left_breast_duration": parse_int(row.get("Left duration (min)", "")),
                "right_breast_duration": parse_int(row.get("Right Duration (min)", "")),
                "volume_offered_ml": "",
                "volume_consumed_ml": "",
                "formula_type": "",
                "food_items": "",
                "appetite": "",
                "notes": row.get("Note", "").strip(),
                "created_at": format_timestamp(start_time),
                "updated_at": format_timestamp(start_time),
            })
        print(f"    Nursing sessions:  {len(nursing_raw):>5}")

    # 2. Pump sessions
    # Ember's pump file has no name prefix (legacy); future babies use {Name}_pump.csv.
    pump_path = (
        SEEDS_DIR / "pump.csv"
        if baby.name == "Ember"
        else SEEDS_DIR / f"{baby.name}_pump.csv"
    )
    pump_raw = read_csv_if_exists(pump_path)
    if pump_raw:
        for row in pump_raw:
            start_time = parse_datetime(row["Time"], baby.datetime_format)
            total_str = row.get("Total duration (min)", "").strip()
            duration_minutes = int(total_str) if total_str else 0
            end_time = start_time + timedelta(minutes=duration_minutes)
            start_side = row.get("Start side", "").strip()
            breast_started = start_side.upper() if start_side in ("Left", "Right") else ""
            total_amount = parse_int(row.get("Total amount (ml)", ""))
            rows.append({
                "id": str(uuid.uuid4()),
                "baby_id": baby_id,
                "start_time": format_timestamp(start_time),
                "end_time": format_timestamp(end_time),
                "feeding_type": "BOTTLE",
                "breast_started": breast_started,
                "left_breast_duration": parse_int(row.get("Left duration (min)", "")),
                "right_breast_duration": parse_int(row.get("Right Duration (min)", "")),
                "volume_offered_ml": total_amount,
                "volume_consumed_ml": total_amount,
                "formula_type": "",
                "food_items": "",
                "appetite": "",
                "notes": row.get("Note", "").strip(),
                "created_at": format_timestamp(start_time),
                "updated_at": format_timestamp(start_time),
            })
        print(f"    Pump sessions:     {len(pump_raw):>5}")

    # 3. Expressed milk feedings
    expressed_raw = read_csv_if_exists(SEEDS_DIR / f"{baby.name}_expressed.csv")
    if expressed_raw:
        for row in expressed_raw:
            start_time = parse_datetime(row["Time"], baby.datetime_format)
            amount = parse_int(row.get("Amount (ml)", ""))
            rows.append({
                "id": str(uuid.uuid4()),
                "baby_id": baby_id,
                "start_time": format_timestamp(start_time),
                "end_time": format_timestamp(start_time),
                "feeding_type": "BOTTLE",
                "breast_started": "",
                "left_breast_duration": "",
                "right_breast_duration": "",
                "volume_offered_ml": amount,
                "volume_consumed_ml": amount,
                "formula_type": "",
                "food_items": "",
                "appetite": "",
                "notes": row.get("Note", "").strip(),
                "created_at": format_timestamp(start_time),
                "updated_at": format_timestamp(start_time),
            })
        print(f"    Expressed:         {len(expressed_raw):>5}")

    print(f"    Feeding total:     {len(rows):>5}")
    return rows


# ---------------------------------------------------------------------------
# Baby profiles
# ---------------------------------------------------------------------------

def create_baby_profiles(babies: list[BabyConfig]) -> list[dict]:
    """Create a baby_profiles row for every entry in babies."""
    now = datetime.now().isoformat()
    return [
        {
            "id": baby.baby_id,
            "name": baby.name,
            "date_of_birth": baby.date_of_birth,
            "birth_weight": baby.birth_weight,
            "birth_length": baby.birth_length,
            "birth_head_circumference": baby.birth_head_circumference,
            "gender": baby.gender,
            "timezone": baby.timezone,
            "notes": baby.notes,
            "created_at": baby.created_at,
            "updated_at": now,
            "is_active": "true",
        }
        for baby in babies
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Transform seed CSVs into raw layer format")
    parser.add_argument(
        "--baby",
        metavar="NAME",
        default=None,
        help="Process only this baby (case-insensitive). Omit to process all.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List configured babies and exit.",
    )
    args = parser.parse_args()

    if args.list:
        print("Configured babies:")
        for baby in BABIES:
            print(f"  {baby.name}  (DOB: {baby.date_of_birth}, id: {baby.baby_id})")
        return

    if args.baby:
        babies = [b for b in BABIES if b.name.lower() == args.baby.lower()]
        if not babies:
            names = [b.name for b in BABIES]
            print(f"Error: baby '{args.baby}' not found. Available: {names}")
            raise SystemExit(1)
    else:
        babies = list(BABIES)

    print("=" * 60)
    print("Seed Data Transformation")
    print("=" * 60)
    print(f"Babies:  {', '.join(b.name for b in babies)}")
    print(f"Input:   {SEEDS_DIR}")
    print(f"Output:  {OUTPUT_DIR}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_diaper:  list[dict] = []
    all_sleep:   list[dict] = []
    all_feeding: list[dict] = []

    for baby in babies:
        print(f"\n  {baby.name}  (id: {baby.baby_id})")
        all_diaper  += transform_diaper_events(baby, baby.baby_id)
        all_sleep   += transform_sleep_sessions(baby, baby.baby_id)
        all_feeding += transform_feeding_sessions(baby, baby.baby_id)

    profiles = create_baby_profiles(babies)

    print("\nWriting output files...")
    write_csv(OUTPUT_DIR / "baby_profiles.csv",    PROFILE_FIELDS,  profiles)
    write_csv(OUTPUT_DIR / "diaper_events.csv",    DIAPER_FIELDS,   all_diaper)
    write_csv(OUTPUT_DIR / "sleep_sessions.csv",   SLEEP_FIELDS,    all_sleep)
    write_csv(OUTPUT_DIR / "feeding_sessions.csv", FEEDING_FIELDS,  all_feeding)

    total = len(profiles) + len(all_diaper) + len(all_sleep) + len(all_feeding)
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  Baby profiles:     {len(profiles):>5}")
    print(f"  Diaper events:     {len(all_diaper):>5}")
    print(f"  Sleep sessions:    {len(all_sleep):>5}")
    print(f"  Feeding sessions:  {len(all_feeding):>5}")
    print(f"  Total records:     {total:>5}")
    print()
    print("Transformation complete!")


if __name__ == "__main__":
    main()
