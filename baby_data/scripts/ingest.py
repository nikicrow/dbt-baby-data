"""
Baby Data Ingestion Pipeline

Full end-to-end pipeline: find zip → extract CSVs → transform → load to DB.
Completely replaces ALL seed data and database records on every run.

Usage:
    # Auto-finds the latest csv*.zip in ~/Downloads:
    python ingest.py

    # Use a specific zip file:
    python ingest.py --zip "C:/Users/nikil/Downloads/csv (3).zip"

    # Load into Supabase instead of local Postgres:
    python ingest.py --target supabase

    # Transform only (skip database load — useful for testing):
    python ingest.py --skip-load

    # Combine flags:
    python ingest.py --zip myfile.zip --skip-load
    python ingest.py --zip myfile.zip --target supabase
"""

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
SEEDS_DIR = SCRIPT_DIR.parent / "seeds"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_latest_zip() -> Path:
    """Auto-detect the most recently modified csv*.zip in ~/Downloads."""
    downloads = Path.home() / "Downloads"
    matches = sorted(
        downloads.glob("csv*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise FileNotFoundError(
            f"No csv*.zip files found in {downloads}.\n"
            "Download the export from the baby tracking app, or pass --zip <path>."
        )
    return matches[0]


def extract_zip(zip_path: Path) -> None:
    """Extract all CSV files from the zip flat into seeds/, overwriting existing files.

    Strips any directory structure inside the zip so every CSV lands directly
    in seeds/ regardless of how the zip was created.
    """
    print(f"Extracting: {zip_path.name}")
    extracted = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if not member.lower().endswith(".csv"):
                continue  # Skip non-CSV files (icons, metadata, etc.)
            filename = Path(member).name  # Strip any zip-internal subdirectories
            target = SEEDS_DIR / filename
            with zf.open(member) as src, open(target, "wb") as dst:
                dst.write(src.read())
            extracted.append(filename)
            print(f"  -> seeds/{filename}")

    if not extracted:
        raise ValueError(f"No CSV files found inside {zip_path.name}")
    print(f"  Extracted {len(extracted)} file(s) into seeds/")


def run_script(script_name: str, extra_args: list[str] | None = None) -> None:
    """Run a sibling script using the same Python interpreter as this process.

    Using sys.executable guarantees the correct virtual environment is used
    rather than whatever 'python' resolves to on the system PATH.
    """
    cmd = [sys.executable, str(SCRIPT_DIR / script_name)] + (extra_args or [])
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\nError: {script_name} exited with code {result.returncode}. Aborting.")
        sys.exit(result.returncode)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Baby data ingestion pipeline: zip → seeds → transform → load"
    )
    parser.add_argument(
        "--zip",
        type=str,
        default=None,
        help="Path to the CSV zip file. If omitted, auto-finds the latest csv*.zip in ~/Downloads.",
    )
    parser.add_argument(
        "--skip-load",
        action="store_true",
        help="Run transform only — skip the database load step.",
    )
    parser.add_argument(
        "--target",
        choices=["local", "supabase"],
        default="local",
        help="Database target for the load step (default: local).",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Baby Data Ingestion Pipeline")
    print("=" * 60)

    # ------------------------------------------------------------------
    # Step 1: Resolve zip path
    # ------------------------------------------------------------------
    if args.zip:
        zip_path = Path(args.zip)
        if not zip_path.exists():
            print(f"Error: Zip file not found: {zip_path}")
            sys.exit(1)
    else:
        print("No --zip specified, searching ~/Downloads for latest csv*.zip...")
        try:
            zip_path = find_latest_zip()
        except FileNotFoundError as e:
            print(f"Error: {e}")
            sys.exit(1)

    print(f"Using:      {zip_path}")
    print(f"Seeds dir:  {SEEDS_DIR}")
    print()

    # ------------------------------------------------------------------
    # Step 2: Extract CSVs from zip into seeds/
    # ------------------------------------------------------------------
    print("--- Step 1/3: Extract ---")
    try:
        extract_zip(zip_path)
    except (zipfile.BadZipFile, ValueError) as e:
        print(f"Error extracting zip: {e}")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 3: Transform seeds → transformed_data/
    # ------------------------------------------------------------------
    print()
    print("--- Step 2/3: Transform ---")
    run_script("transform_seeds.py")

    # ------------------------------------------------------------------
    # Step 4: Load transformed_data/ → PostgreSQL
    # ------------------------------------------------------------------
    if args.skip_load:
        print()
        print("--- Step 3/3: Load (skipped via --skip-load) ---")
        print()
        print("=" * 60)
        print("Ingest complete (transform only — no DB changes made).")
        print("=" * 60)
        return

    print()
    print(f"--- Step 3/3: Load (target: {args.target}) ---")
    run_script("load_to_database.py", ["--target", args.target, "--force"])

    print()
    print("=" * 60)
    print(f"Ingest complete! Database fully refreshed (target: {args.target}).")
    print("=" * 60)


if __name__ == "__main__":
    main()
