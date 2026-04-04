"""
Baby Data Pipeline Runner

Orchestrates the full pipeline: transform seed CSVs -> load to PostgreSQL.

Usage:
    python run_pipeline.py                         # all babies, full run
    python run_pipeline.py --baby Imogen           # single baby
    python run_pipeline.py --dry-run               # show what would happen
    python run_pipeline.py --baby Ember --dry-run  # dry-run for one baby
    python run_pipeline.py --skip-load             # transform only, no DB

Environment variables required for the load step:
    DATABASE_URL="postgresql://user:pass@host:port/dbname"
    OR: DB_HOST, DB_NAME, DB_USER, DB_PASSWORD (and optionally DB_PORT, DB_SCHEMA)
"""

import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Ensure siblings are importable
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from transform_seeds import BABIES  # noqa: E402 — after sys.path insert


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("pipeline")


def run_step(
    label: str,
    cmd: list[str],
    dry_run: bool,
    log: logging.Logger,
) -> bool:
    """Run a subprocess step. Returns True on success."""
    log.info(f"{'─' * 50}")
    log.info(f"  {label}")
    log.info(f"  cmd: {' '.join(cmd)}")

    if dry_run:
        log.info("  [DRY RUN] skipping execution")
        return True

    result = subprocess.run(cmd)
    if result.returncode != 0:
        log.error(f"  Step failed with exit code {result.returncode}")
        return False

    log.info(f"  Step complete.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the full baby data pipeline: transform -> load",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--baby",
        metavar="NAME",
        help="Process only this baby (case-insensitive). Omit for all babies.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without writing anything.",
    )
    parser.add_argument(
        "--skip-load",
        action="store_true",
        help="Run transform only; skip the database load step.",
    )
    args = parser.parse_args()

    log = setup_logging()

    # ------------------------------------------------------------------ banner
    log.info("=" * 60)
    log.info("  Baby Data Pipeline Runner")
    log.info("=" * 60)

    # Validate --baby filter
    if args.baby:
        matched = [b for b in BABIES if b.name.lower() == args.baby.lower()]
        if not matched:
            known = [b.name for b in BABIES]
            log.error(f"Unknown baby '{args.baby}'. Configured babies: {known}")
            sys.exit(1)
        babies_to_run = matched
    else:
        babies_to_run = list(BABIES)

    log.info(f"  Babies:   {[b.name for b in babies_to_run]}")
    log.info(f"  Mode:     {'DRY RUN' if args.dry_run else 'live'}")
    log.info(f"  Load DB:  {'no (--skip-load)' if args.skip_load else 'yes'}")
    log.info("")

    start = datetime.now()

    # ---------------------------------------------------------------- step 1: transform
    transform_cmd = [sys.executable, str(SCRIPT_DIR / "transform_seeds.py")]
    if args.baby:
        transform_cmd += ["--baby", args.baby]

    ok = run_step(
        label="Step 1/2 — Transform seeds",
        cmd=transform_cmd,
        dry_run=args.dry_run,
        log=log,
    )
    if not ok:
        log.error("Pipeline aborted at transform step.")
        sys.exit(1)

    # ---------------------------------------------------------------- step 2: load
    if args.skip_load:
        log.info("")
        log.info("  Skipping database load (--skip-load).")
    else:
        log.info("")
        load_cmd = [sys.executable, str(SCRIPT_DIR / "load_to_database.py"), "--force"]

        ok = run_step(
            label="Step 2/2 — Load to database",
            cmd=load_cmd,
            dry_run=args.dry_run,
            log=log,
        )
        if not ok:
            log.error("Pipeline aborted at load step.")
            sys.exit(1)

    # ---------------------------------------------------------------- summary
    elapsed = (datetime.now() - start).total_seconds()
    log.info("")
    log.info("=" * 60)
    log.info(f"  Pipeline complete in {elapsed:.1f}s")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
