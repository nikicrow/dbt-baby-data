"""
Seed Data Transformation Script

Transforms historical baby tracking CSV data from the seeds/ folder
into the raw layer schema format, outputting to transformed_data/.

Usage:
    python transform_seeds.py
"""

import csv
import uuid
from datetime import datetime, timedelta
from pathlib import Path
import json

# Paths
SCRIPT_DIR = Path(__file__).parent
SEEDS_DIR = SCRIPT_DIR.parent / "seeds"
OUTPUT_DIR = SCRIPT_DIR.parent / "transformed_data"

# Baby profile - fixed UUID for foreign key consistency
BABY_ID = str(uuid.uuid4())
BABY_PROFILE = {
    'id': BABY_ID,
    'name': 'Ember',
    'date_of_birth': '2023-08-18',
    'birth_weight': '',
    'birth_length': '',
    'birth_head_circumference': '',
    'gender': 'female',
    'timezone': 'Australia/Sydney',
    'notes': 'Migrated from seed data',
    'created_at': '2023-08-18T00:00:00',
    'updated_at': datetime.now().isoformat(),
    'is_active': 'true'
}


def parse_datetime(date_str: str) -> datetime:
    """Parse date string like '9/3/23 6:03 PM' to datetime."""
    return datetime.strptime(date_str.strip(), "%m/%d/%y %I:%M %p")


def format_timestamp(dt: datetime) -> str:
    """Format datetime to ISO timestamp without timezone."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_int(value: str) -> str:
    """Parse integer value, handling empty strings and commas."""
    if not value or value.strip() == '':
        return ''
    # Remove commas from numbers like "1,331"
    return str(int(value.replace(',', '').strip()))


def parse_float(value: str) -> str:
    """Parse float value, handling empty strings and commas."""
    if not value or value.strip() == '':
        return ''
    return str(float(value.replace(',', '').strip()))


def infer_sleep_type(start_time: datetime, duration_minutes: int) -> str:
    """
    Infer sleep type based on time and duration.
    NIGHTTIME: duration > 180 min (3 hours) AND start time between 7PM-7AM
    Otherwise: NAP
    """
    hour = start_time.hour
    is_night_hours = hour >= 19 or hour < 7  # 7PM to 7AM
    is_long_sleep = duration_minutes > 180

    if is_night_hours and is_long_sleep:
        return 'NIGHTTIME'
    return 'NAP'


def transform_diaper_events():
    """Transform Ember_diaper.csv to diaper_events.csv"""
    input_file = SEEDS_DIR / "Ember_diaper.csv"
    output_file = OUTPUT_DIR / "diaper_events.csv"

    rows = []
    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            timestamp = parse_datetime(row['Time'])
            status = row['Status'].strip()

            # Map status to has_urine/has_stool
            has_urine = status in ['Wet', 'Mixed']
            has_stool = status in ['Mixed', 'Dirty']

            rows.append({
                'id': str(uuid.uuid4()),
                'baby_id': BABY_ID,
                'timestamp': format_timestamp(timestamp),
                'has_urine': str(has_urine).lower(),
                'urine_volume': 'MODERATE' if has_urine else 'NONE',
                'has_stool': str(has_stool).lower(),
                'stool_consistency': 'SOFT' if has_stool else '',
                'stool_color': 'YELLOW' if has_stool else '',
                'diaper_type': 'DISPOSABLE',
                'notes': row.get('Note', '').strip(),
                'created_at': format_timestamp(timestamp),
                'updated_at': format_timestamp(timestamp)
            })

    # Write output
    fieldnames = ['id', 'baby_id', 'timestamp', 'has_urine', 'urine_volume',
                  'has_stool', 'stool_consistency', 'stool_color', 'diaper_type',
                  'notes', 'created_at', 'updated_at']

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Transformed {len(rows)} diaper events -> {output_file}")
    return len(rows)


def transform_sleep_sessions():
    """Transform Ember_sleep.csv to sleep_sessions.csv"""
    input_file = SEEDS_DIR / "Ember_sleep.csv"
    output_file = OUTPUT_DIR / "sleep_sessions.csv"

    rows = []
    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            start_time = parse_datetime(row['Time'])
            duration_str = row['Duration (min)'].strip()

            if not duration_str:
                continue

            duration_minutes = int(duration_str)
            end_time = start_time + timedelta(minutes=duration_minutes)
            sleep_type = infer_sleep_type(start_time, duration_minutes)

            rows.append({
                'id': str(uuid.uuid4()),
                'baby_id': BABY_ID,
                'start_time': format_timestamp(start_time),
                'end_time': format_timestamp(end_time),
                'sleep_type': sleep_type,
                'location': 'CRIB',
                'sleep_quality': '',
                'sleep_environment': '',
                'wake_reason': 'NATURAL',
                'notes': row.get('Note', '').strip(),
                'created_at': format_timestamp(start_time),
                'updated_at': format_timestamp(start_time)
            })

    fieldnames = ['id', 'baby_id', 'start_time', 'end_time', 'sleep_type',
                  'location', 'sleep_quality', 'sleep_environment', 'wake_reason',
                  'notes', 'created_at', 'updated_at']

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Transformed {len(rows)} sleep sessions -> {output_file}")
    return len(rows)


def transform_feeding_sessions():
    """
    Transform nursing, pump, and expressed CSVs into feeding_sessions.csv
    - Nursing -> feeding_type: breast
    - Pump -> feeding_type: bottle (pumped milk fed to baby)
    - Expressed -> feeding_type: bottle (expressed milk fed to baby)
    """
    output_file = OUTPUT_DIR / "feeding_sessions.csv"
    rows = []

    # 1. Transform nursing sessions
    nursing_file = SEEDS_DIR / "Ember_nursing.csv"
    with open(nursing_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            start_time = parse_datetime(row['Time'])
            total_str = row.get('Total (min)', '').strip()

            if total_str:
                duration_minutes = int(total_str)
                end_time = start_time + timedelta(minutes=duration_minutes)
            else:
                end_time = start_time

            # Map start side (uppercase for PostgreSQL enum)
            start_side = row.get('Start side', '').strip()
            breast_started = start_side.upper() if start_side in ['Left', 'Right'] else ''

            rows.append({
                'id': str(uuid.uuid4()),
                'baby_id': BABY_ID,
                'start_time': format_timestamp(start_time),
                'end_time': format_timestamp(end_time),
                'feeding_type': 'BREAST',
                'breast_started': breast_started,
                'left_breast_duration': parse_int(row.get('Left duration (min)', '')),
                'right_breast_duration': parse_int(row.get('Right Duration (min)', '')),
                'volume_offered_ml': '',
                'volume_consumed_ml': '',
                'formula_type': '',
                'food_items': '',
                'appetite': '',
                'notes': row.get('Note', '').strip(),
                'created_at': format_timestamp(start_time),
                'updated_at': format_timestamp(start_time)
            })

    nursing_count = len(rows)
    print(f"  - Nursing: {nursing_count} rows")

    # 2. Transform pump sessions (baby fed pumped milk)
    pump_file = SEEDS_DIR / "Ember_pump.csv"
    pump_count = 0
    with open(pump_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            start_time = parse_datetime(row['Time'])
            total_str = row.get('Total duration (min)', '').strip()

            if total_str:
                duration_minutes = int(total_str)
                end_time = start_time + timedelta(minutes=duration_minutes)
            else:
                end_time = start_time

            start_side = row.get('Start side', '').strip()
            breast_started = start_side.upper() if start_side in ['Left', 'Right'] else ''

            # Get total amount if available
            total_amount = parse_int(row.get('Total amount (ml)', ''))

            rows.append({
                'id': str(uuid.uuid4()),
                'baby_id': BABY_ID,
                'start_time': format_timestamp(start_time),
                'end_time': format_timestamp(end_time),
                'feeding_type': 'BOTTLE',
                'breast_started': breast_started,
                'left_breast_duration': parse_int(row.get('Left duration (min)', '')),
                'right_breast_duration': parse_int(row.get('Right Duration (min)', '')),
                'volume_offered_ml': total_amount,
                'volume_consumed_ml': total_amount,  # Assume all consumed
                'formula_type': '',
                'food_items': '',
                'appetite': '',
                'notes': row.get('Note', '').strip(),
                'created_at': format_timestamp(start_time),
                'updated_at': format_timestamp(start_time)
            })
            pump_count += 1

    print(f"  - Pump: {pump_count} rows")

    # 3. Transform expressed milk feedings
    expressed_file = SEEDS_DIR / "Ember_expressed.csv"
    expressed_count = 0
    with open(expressed_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            start_time = parse_datetime(row['Time'])
            amount = parse_int(row.get('Amount (ml)', ''))

            rows.append({
                'id': str(uuid.uuid4()),
                'baby_id': BABY_ID,
                'start_time': format_timestamp(start_time),
                'end_time': format_timestamp(start_time),  # No duration info
                'feeding_type': 'BOTTLE',
                'breast_started': '',
                'left_breast_duration': '',
                'right_breast_duration': '',
                'volume_offered_ml': amount,
                'volume_consumed_ml': amount,  # Assume all consumed
                'formula_type': '',
                'food_items': '',
                'appetite': '',
                'notes': row.get('Note', '').strip(),
                'created_at': format_timestamp(start_time),
                'updated_at': format_timestamp(start_time)
            })
            expressed_count += 1

    print(f"  - Expressed: {expressed_count} rows")

    fieldnames = ['id', 'baby_id', 'start_time', 'end_time', 'feeding_type',
                  'breast_started', 'left_breast_duration', 'right_breast_duration',
                  'volume_offered_ml', 'volume_consumed_ml', 'formula_type',
                  'food_items', 'appetite', 'notes', 'created_at', 'updated_at']

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    print(f"Transformed {total} feeding sessions -> {output_file}")
    return total


def create_baby_profile():
    """Create baby_profiles.csv with Ember's profile."""
    output_file = OUTPUT_DIR / "baby_profiles.csv"

    fieldnames = ['id', 'name', 'date_of_birth', 'birth_weight', 'birth_length',
                  'birth_head_circumference', 'gender', 'timezone', 'notes',
                  'created_at', 'updated_at', 'is_active']

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(BABY_PROFILE)

    print(f"Created baby profile -> {output_file}")
    print(f"  Baby ID: {BABY_ID}")


def main():
    """Run all transformations."""
    print("=" * 60)
    print("Seed Data Transformation")
    print("=" * 60)
    print(f"Input:  {SEEDS_DIR}")
    print(f"Output: {OUTPUT_DIR}")
    print()

    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Create baby profile first (generates BABY_ID used by all other transforms)
    create_baby_profile()
    print()

    # Transform each data type
    diaper_count = transform_diaper_events()
    print()

    sleep_count = transform_sleep_sessions()
    print()

    feeding_count = transform_feeding_sessions()
    print()

    # Summary
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Baby profiles:    1")
    print(f"Diaper events:    {diaper_count}")
    print(f"Sleep sessions:   {sleep_count}")
    print(f"Feeding sessions: {feeding_count}")
    print(f"Total records:    {1 + diaper_count + sleep_count + feeding_count}")
    print()
    print("Transformation complete!")


if __name__ == "__main__":
    main()
