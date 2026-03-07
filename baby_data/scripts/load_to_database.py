"""
Database Load Script

Loads transformed CSV data directly into the PostgreSQL database.
Reads from transformed_data/ and inserts into the baby_app schema.

Usage:
    # Set environment variables first:
    export DATABASE_URL="postgresql://user:pass@host:port/dbname"
    # OR set individual variables:
    export DB_HOST="localhost"
    export DB_PORT="5432"
    export DB_NAME="baby_data"
    export DB_USER="postgres"
    export DB_PASSWORD="your_password"
    export DB_SCHEMA="public"  # Optional, defaults to "public"

    python load_to_database.py
    python load_to_database.py --force  # Clear existing data without prompting
"""

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print("Error: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

# Paths
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "transformed_data"


def get_connection_params() -> dict:
    """Get database connection parameters from environment variables."""
    # Try DATABASE_URL first (common in production/Heroku/Railway)
    database_url = os.environ.get('DATABASE_URL')
    if database_url:
        parsed = urlparse(database_url)
        return {
            'host': parsed.hostname,
            'port': parsed.port or 5432,
            'database': parsed.path[1:],  # Remove leading /
            'user': parsed.username,
            'password': parsed.password,
        }

    # Fall back to individual variables
    required = ['DB_HOST', 'DB_NAME', 'DB_USER', 'DB_PASSWORD']
    missing = [var for var in required if not os.environ.get(var)]
    if missing:
        print(f"Error: Missing environment variables: {', '.join(missing)}")
        print("\nSet DATABASE_URL or individual DB_* variables.")
        print("Example:")
        print('  export DATABASE_URL="postgresql://user:pass@localhost:5432/dbname"')
        sys.exit(1)

    return {
        'host': os.environ['DB_HOST'],
        'port': int(os.environ.get('DB_PORT', 5432)),
        'database': os.environ['DB_NAME'],
        'user': os.environ['DB_USER'],
        'password': os.environ['DB_PASSWORD'],
    }


def get_schema() -> str:
    """Get the target schema name."""
    return os.environ.get('DB_SCHEMA', 'public')


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


def insert_table(conn, table_name: str, csv_filename: str) -> int:
    """Insert data from CSV into database table."""
    schema = get_schema()
    headers, rows = read_csv(csv_filename)

    if not rows:
        print(f"  No data to insert for {table_name}")
        return 0

    # Build column list and values
    columns = headers
    values = []
    for row in rows:
        converted_row = tuple(convert_value(row[col], col) for col in columns)
        values.append(converted_row)

    # Build INSERT query
    col_str = ', '.join(f'"{col}"' for col in columns)
    placeholders = ', '.join(['%s'] * len(columns))
    query = f'INSERT INTO {schema}.{table_name} ({col_str}) VALUES %s'

    # Execute batch insert
    with conn.cursor() as cur:
        execute_values(cur, query, values, template=f'({placeholders})')

    conn.commit()
    return len(values)


def check_table_exists(conn, table_name: str) -> bool:
    """Check if a table exists in the database."""
    schema = get_schema()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
            )
        """, (schema, table_name))
        return cur.fetchone()[0]


def get_table_row_count(conn, table_name: str) -> int:
    """Get current row count in table."""
    schema = get_schema()
    with conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM {schema}.{table_name}')
        return cur.fetchone()[0]


def clear_table(conn, table_name: str) -> int:
    """Clear all rows from table. Returns count of deleted rows."""
    schema = get_schema()
    with conn.cursor() as cur:
        cur.execute(f'DELETE FROM {schema}.{table_name}')
        count = cur.rowcount
    conn.commit()
    return count


def main():
    """Load all transformed data into the database."""
    parser = argparse.ArgumentParser(description='Load transformed data into PostgreSQL')
    parser.add_argument('--force', '-f', action='store_true',
                        help='Clear existing data without prompting')
    args = parser.parse_args()

    print("=" * 60)
    print("Database Load Script")
    print("=" * 60)
    print(f"Data source: {DATA_DIR}")
    print()

    # Check data files exist
    csv_files = list(DATA_DIR.glob("*.csv"))
    if not csv_files:
        print(f"Error: No CSV files found in {DATA_DIR}")
        print("Run transform_seeds.py first to generate the data.")
        sys.exit(1)

    # Connect to database
    params = get_connection_params()
    schema = get_schema()
    print(f"Connecting to: {params['host']}:{params['port']}/{params['database']}")
    print(f"Target schema: {schema}")
    print()

    try:
        conn = psycopg2.connect(**params)
        print("Connected successfully!")
        print()
    except psycopg2.Error as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    # Table mapping: CSV filename -> database table name
    # Order matters for foreign key constraints (baby_profiles must be first for insert)
    tables = [
        ('baby_profiles.csv', 'baby_profiles'),
        ('diaper_events.csv', 'diaper_events'),
        ('sleep_sessions.csv', 'sleep_sessions'),
        ('feeding_sessions.csv', 'feeding_sessions'),
    ]

    # All tables that reference baby_profiles (for clearing in correct order)
    all_dependent_tables = [
        'health_events',
        'growth_measurements',
        'feeding_sessions',
        'sleep_sessions',
        'diaper_events',
        'baby_profiles',  # Clear last
    ]

    # Check tables exist
    print("Checking tables...")
    for csv_file, table_name in tables:
        exists = check_table_exists(conn, table_name)
        status = "OK" if exists else "MISSING"
        print(f"  {table_name}: {status}")

    missing = [t for _, t in tables if not check_table_exists(conn, t)]
    if missing:
        print(f"\nError: Missing tables: {', '.join(missing)}")
        print("Please ensure the database schema is set up correctly.")
        conn.close()
        sys.exit(1)

    print()

    # Check for existing data
    print("Checking for existing data...")
    for csv_file, table_name in tables:
        count = get_table_row_count(conn, table_name)
        if count > 0:
            print(f"  {table_name}: {count} existing rows")

    existing_data = any(get_table_row_count(conn, t) > 0 for _, t in tables)
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
        # Clear all dependent tables in correct order for foreign key constraints
        for table_name in all_dependent_tables:
            if check_table_exists(conn, table_name):
                deleted = clear_table(conn, table_name)
                if deleted > 0:
                    print(f"  Cleared {deleted} rows from {table_name}")

    print()
    print("Loading data...")

    # Insert data
    total = 0
    for csv_file, table_name in tables:
        count = insert_table(conn, table_name, csv_file)
        print(f"  {table_name}: {count} rows inserted")
        total += count

    print()
    print("=" * 60)
    print(f"Total: {total} records loaded successfully!")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
