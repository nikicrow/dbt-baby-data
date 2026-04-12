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

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print("Error: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

try:
    from pydantic import Field
    from pydantic_settings import BaseSettings
except ImportError:
    print("Error: pydantic-settings not installed. Run: pip install pydantic-settings")
    sys.exit(1)

# Paths
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "transformed_data"
ENV_FILE = SCRIPT_DIR / ".env"


class LocalDatabaseConfig(BaseSettings):
    host: str = Field(default="localhost", alias="DB_HOST")
    port: int = Field(default=5432, alias="DB_PORT")
    dbname: str = Field(default="baby_data", alias="DB_NAME")
    user: str = Field(default="postgres", alias="DB_USER")
    password: str = Field(default="", alias="DB_PASSWORD")
    sslmode: str = Field(default="disable", alias="DB_SSLMODE")
    schema: str = Field(default="public", alias="DB_SCHEMA")

    model_config = {"env_file": str(ENV_FILE), "populate_by_name": True}


class SupabaseDatabaseConfig(BaseSettings):
    host: str = Field(alias="SUPABASE_DB_HOST")
    port: int = Field(default=5432, alias="SUPABASE_DB_PORT")
    dbname: str = Field(default="postgres", alias="SUPABASE_DB_NAME")
    user: str = Field(default="postgres", alias="SUPABASE_DB_USER")
    password: str = Field(alias="SUPABASE_DB_PASSWORD")
    sslmode: str = Field(default="require", alias="SUPABASE_DB_SSLMODE")
    schema: str = Field(default="baby_data", alias="SUPABASE_DB_SCHEMA")

    model_config = {"env_file": str(ENV_FILE), "populate_by_name": True}


def get_config(target: str) -> LocalDatabaseConfig | SupabaseDatabaseConfig:
    if target == "supabase":
        return SupabaseDatabaseConfig()
    return LocalDatabaseConfig()


def get_connection_string(config: LocalDatabaseConfig | SupabaseDatabaseConfig) -> str:
    return (
        f"postgresql://{config.user}:{config.password}"
        f"@{config.host}:{config.port}/{config.dbname}"
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


def insert_table(conn, table_name: str, csv_filename: str, schema: str) -> int:
    """Insert data from CSV into database table."""
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


def get_table_row_count(conn, table_name: str, schema: str) -> int:
    """Get current row count in table."""
    with conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM {schema}.{table_name}')
        return cur.fetchone()[0]


def clear_table(conn, table_name: str, schema: str) -> int:
    """Clear all rows from table. Returns count of deleted rows."""
    with conn.cursor() as cur:
        cur.execute(f'DELETE FROM {schema}.{table_name}')
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

    print(f"Connecting to: {config.host}:{config.port}/{config.dbname}")
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

    all_dependent_tables = [
        'health_events',
        'growth_measurements',
        'feeding_sessions',
        'sleep_sessions',
        'diaper_events',
        'baby_profiles',
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

    print("Checking for existing data...")
    for csv_file, table_name in tables:
        count = get_table_row_count(conn, table_name, schema)
        if count > 0:
            print(f"  {table_name}: {count} existing rows")

    existing_data = any(get_table_row_count(conn, t, schema) > 0 for _, t in tables)
    if existing_data:
        if not args.force:
            print()
            response = input("Tables contain existing data. Clear and reload? [y/N]: ")
            if response.lower() != 'y':
                print("Aborted. No changes made.")
                conn.close()
                sys.exit(0)

        print()
        print("Clearing existing data...")
        for table_name in all_dependent_tables:
            if check_table_exists(conn, table_name, schema):
                deleted = clear_table(conn, table_name, schema)
                if deleted > 0:
                    print(f"  Cleared {deleted} rows from {table_name}")

    print()
    print("Loading data...")

    total = 0
    for csv_file, table_name in tables:
        count = insert_table(conn, table_name, csv_file, schema)
        print(f"  {table_name}: {count} rows inserted")
        total += count

    print()
    print("=" * 60)
    print(f"Total: {total} records loaded successfully!")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
