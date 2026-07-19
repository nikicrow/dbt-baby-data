"""
Database Load Script

Loads transformed CSV data directly into the PostgreSQL database.
Reads from transformed_data/ and inserts into the configured schema.

Usage:
    # Local (default):
    python load_to_database.py

    # Supabase:
    python load_to_database.py --target supabase

    # Force reload without prompt:
    python load_to_database.py --force
    python load_to_database.py --target supabase --force

Environment variables (local target):
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_SSLMODE

Environment variables (supabase target):
    SUPABASE_DB_HOST, SUPABASE_DB_PORT, SUPABASE_DB_NAME,
    SUPABASE_DB_USER, SUPABASE_DB_PASSWORD
"""

import argparse
import csv
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print("Error: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

try:
    from pydantic_settings import BaseSettings
except ImportError:
    print("Error: pydantic-settings not installed. Run: pip install pydantic-settings")
    sys.exit(1)

# Paths
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "transformed_data"
ENV_FILE = SCRIPT_DIR / ".env"

# Rows written by this pipeline carry source='ingested' (set in
# transform_seeds.py). The loader only ever deletes rows with this tag —
# rows created through the app (source='app') are never touched.
PIPELINE_SOURCE = "ingested"


class DatabaseConfig(BaseSettings):
    """Shared connection settings; subclasses set the env var prefix and target-specific defaults."""

    host: str = "localhost"
    port: int = 5432
    name: str = "baby_data"
    user: str = "postgres"
    password: str = ""
    sslmode: str = "disable"
    schema: str = "public"

    # extra="ignore": the shared .env holds both the DB_* and SUPABASE_DB_*
    # blocks, so whichever config is loading must ignore the other prefix's
    # keys rather than rejecting them as unexpected extras.
    model_config = {"env_file": str(ENV_FILE), "extra": "ignore"}


class LocalDatabaseConfig(DatabaseConfig):
    model_config = {"env_prefix": "DB_"}


class SupabaseDatabaseConfig(DatabaseConfig):
    host: str
    name: str = "postgres"
    password: str
    sslmode: str = "require"
    # The app's Alembic migrations create its tables in public, same as local.
    schema: str = "public"

    model_config = {"env_prefix": "SUPABASE_DB_"}


def get_config(target: str) -> DatabaseConfig:
    if target == "supabase":
        return SupabaseDatabaseConfig()
    return LocalDatabaseConfig()


def get_connection_string(config: DatabaseConfig) -> str:
    # Percent-encode credentials so a password (or user) containing URL-special
    # characters like @ : / ? # can't corrupt the connection URI. Supabase
    # database passwords often include such characters.
    user = quote(config.user, safe="")
    password = quote(config.password, safe="")
    return (
        f"postgresql://{user}:{password}"
        f"@{config.host}:{config.port}/{config.name}"
        f"?sslmode={config.sslmode}"
    )


def read_csv(filename: str) -> tuple[list[str], list[dict]]:
    """Read CSV file and return headers and rows."""
    filepath = DATA_DIR / filename
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)
    return headers, rows


def convert_value(value: str, column: str) -> Any:
    """Convert CSV string value to appropriate Python/PostgreSQL type."""
    if value == '' or value is None:
        return None

    # Boolean columns
    if column in ('has_urine', 'has_stool', 'is_active', 'follow_up_required'):
        return value.lower() == 'true'

    # Integer columns
    int_columns = ('left_breast_duration', 'right_breast_duration',
                   'volume_offered_ml', 'volume_consumed_ml')
    if column in int_columns:
        return int(value) if value else None

    # Float columns
    float_columns = ('birth_weight', 'birth_length', 'birth_head_circumference',
                     'weight_kg', 'length_cm', 'head_circumference_cm',
                     'temperature_celsius')
    if column in float_columns:
        return float(value) if value else None

    return value


def insert_table(conn, table_name: str, csv_filename: str, schema: str,
                 conflict_key: str | None = None) -> int:
    """Insert data from CSV into database table.

    If conflict_key is given, rows are upserted (ON CONFLICT DO UPDATE)
    instead of plain-inserted — used for baby_profiles, whose deterministic
    ids persist across ingests and may be referenced by app-created events.
    """
    headers, rows = read_csv(csv_filename)

    if not rows:
        print(f"  No data to insert for {table_name}")
        return 0

    columns = headers
    values = []
    for row in rows:
        converted_row = tuple(convert_value(row[col], col) for col in columns)
        values.append(converted_row)

    col_str = ', '.join(f'"{col}"' for col in columns)
    placeholders = ', '.join(['%s'] * len(columns))
    query = f'INSERT INTO {schema}.{table_name} ({col_str}) VALUES %s'
    if conflict_key:
        update_cols = [col for col in columns if col != conflict_key]
        set_str = ', '.join(f'"{col}" = EXCLUDED."{col}"' for col in update_cols)
        query += f' ON CONFLICT ("{conflict_key}") DO UPDATE SET {set_str}'

    with conn.cursor() as cur:
        execute_values(cur, query, values, template=f'({placeholders})')

    conn.commit()
    return len(values)


def check_table_exists(conn, table_name: str, schema: str) -> bool:
    """Check if a table exists in the database."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
            )
        """, (schema, table_name))
        return cur.fetchone()[0]


def get_ingested_row_count(conn, table_name: str, schema: str) -> int:
    """Count rows previously loaded by this pipeline (source='ingested')."""
    with conn.cursor() as cur:
        cur.execute(
            f'SELECT COUNT(*) FROM {schema}.{table_name} WHERE source = %s',
            (PIPELINE_SOURCE,),
        )
        return cur.fetchone()[0]


def clear_ingested_rows(conn, table_name: str, schema: str) -> int:
    """Delete only pipeline-loaded rows. Returns count of deleted rows."""
    with conn.cursor() as cur:
        cur.execute(
            f'DELETE FROM {schema}.{table_name} WHERE source = %s',
            (PIPELINE_SOURCE,),
        )
        count = cur.rowcount
    conn.commit()
    return count


def main():
    """Load all transformed data into the database."""
    parser = argparse.ArgumentParser(description='Load transformed data into PostgreSQL')
    parser.add_argument('--target', choices=['local', 'supabase'], default='local',
                        help='Database target (default: local)')
    parser.add_argument('--force', '-f', action='store_true',
                        help='Clear existing data without prompting')
    args = parser.parse_args()

    print("=" * 60)
    print("Database Load Script")
    print("=" * 60)
    print(f"Target: {args.target}")
    print(f"Data source: {DATA_DIR}")
    print()

    csv_files = list(DATA_DIR.glob("*.csv"))
    if not csv_files:
        print(f"Error: No CSV files found in {DATA_DIR}")
        print("Run transform_seeds.py first to generate the data.")
        sys.exit(1)

    config = get_config(args.target)
    schema = config.schema
    conn_str = get_connection_string(config)

    print(f"Connecting to: {config.host}:{config.port}/{config.name}")
    print(f"Target schema: {schema}")
    print()

    try:
        conn = psycopg2.connect(conn_str)
        print("Connected successfully!")
        print()
    except psycopg2.Error as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    tables = [
        ('baby_profiles.csv', 'baby_profiles'),
        ('diaper_events.csv', 'diaper_events'),
        ('sleep_sessions.csv', 'sleep_sessions'),
        ('feeding_sessions.csv', 'feeding_sessions'),
    ]

    # Only pipeline-owned rows (source='ingested') in these tables are ever
    # deleted. baby_profiles is upserted instead — its rows may be referenced
    # by app-created events. App-only tables (growth_measurements,
    # health_events) are never touched.
    event_tables = [
        'feeding_sessions',
        'sleep_sessions',
        'diaper_events',
    ]

    print("Checking tables...")
    for csv_file, table_name in tables:
        exists = check_table_exists(conn, table_name, schema)
        status = "OK" if exists else "MISSING"
        print(f"  {table_name}: {status}")

    missing = [t for _, t in tables if not check_table_exists(conn, t, schema)]
    if missing:
        print(f"\nError: Missing tables: {', '.join(missing)}")
        print("Please ensure the database schema is set up correctly.")
        conn.close()
        sys.exit(1)

    print()

    print("Checking for previously ingested data...")
    for table_name in event_tables:
        count = get_ingested_row_count(conn, table_name, schema)
        if count > 0:
            print(f"  {table_name}: {count} ingested rows")

    existing_data = any(get_ingested_row_count(conn, t, schema) > 0 for t in event_tables)
    if existing_data:
        if not args.force:
            print()
            response = input("Replace previously ingested rows? App-created rows are kept. [y/N]: ")
            if response.lower() != 'y':
                print("Aborted. No changes made.")
                conn.close()
                sys.exit(0)

        print()
        print("Clearing previously ingested rows...")
        for table_name in event_tables:
            deleted = clear_ingested_rows(conn, table_name, schema)
            if deleted > 0:
                print(f"  Cleared {deleted} ingested rows from {table_name}")

    print()
    print("Loading data...")

    total = 0
    for csv_file, table_name in tables:
        conflict_key = 'id' if table_name == 'baby_profiles' else None
        count = insert_table(conn, table_name, csv_file, schema, conflict_key=conflict_key)
        print(f"  {table_name}: {count} rows inserted")
        total += count

    print()
    print("=" * 60)
    print(f"Total: {total} records loaded successfully!")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
