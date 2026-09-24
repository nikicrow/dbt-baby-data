"""Pull `ml.ml_sleep_training_set` once, then work from a local parquet file.

Every experiment should read the same frozen snapshot, not the live table:
app entries keep landing in Postgres, and a model comparison where the rows
changed between two runs isn't a comparison. Snapshots are timestamped and
never overwritten, so an old result can always be traced back to its data.
"""

from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

from baby_ml.settings import ML_DIR, DatabaseSettings

SNAPSHOT_DIR = ML_DIR / "data"
SNAPSHOT_PREFIX = "ml_sleep_training_set_"

# Ordered so a snapshot's row order is deterministic, which keeps anything
# order-sensitive downstream (e.g. a seeded bootstrap) reproducible.
QUERY = """
select *
from ml.ml_sleep_training_set
order by baby_name, prediction_time
"""

# Text columns with a small fixed vocabulary. As pandas `category` they go
# straight into LightGBM / XGBoost (enable_categorical=True) with no encoding
# step; they need one-hot encoding for logistic regression.
CATEGORICAL_COLUMNS = ("baby_name", "last_feed_type", "last_feed_breast_side")


def pull_snapshot(settings: DatabaseSettings | None = None) -> Path:
    """Query the database and write a new timestamped snapshot. Needs the tunnel."""
    settings = settings or DatabaseSettings()
    engine = create_engine(settings.url, connect_args={"connect_timeout": 10})
    try:
        with engine.connect() as conn:
            # coerce_float turns Postgres `numeric` (Decimal) into float64
            # rather than leaving an object column.
            df = pd.read_sql(QUERY, conn, coerce_float=True)
    finally:
        engine.dispose()

    df = _coerce_dtypes(df)
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    path = SNAPSHOT_DIR / f"{SNAPSHOT_PREFIX}{datetime.now():%Y%m%dT%H%M%S}.parquet"
    df.to_parquet(path, index=False)
    return path


def latest_snapshot() -> Path | None:
    """The newest snapshot on disk, or None if nothing has been pulled yet."""
    # The timestamp format sorts lexically in time order.
    snapshots = sorted(SNAPSHOT_DIR.glob(f"{SNAPSHOT_PREFIX}*.parquet"))
    return snapshots[-1] if snapshots else None


def load_training_set(refresh: bool = False, path: Path | None = None) -> pd.DataFrame:
    """Load the training set from a snapshot, pulling a first one if needed.

    - `path` pins an exact snapshot, for reproducing an earlier result.
    - `refresh=True` pulls a new snapshot from the database first.
    - Otherwise the newest snapshot on disk is used, and the database is only
      touched when there isn't one yet.
    """
    if path is None:
        path = pull_snapshot() if refresh else (latest_snapshot() or pull_snapshot())
    df = pd.read_parquet(path)
    df.attrs["snapshot"] = path.name
    return df


def _coerce_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # uuid.UUID objects can't be written to parquet.
    df["baby_id"] = df["baby_id"].astype(str)
    df["prediction_time"] = pd.to_datetime(df["prediction_time"])
    df["calendar_date"] = pd.to_datetime(df["calendar_date"])
    for column in CATEGORICAL_COLUMNS:
        df[column] = df[column].astype("category")
    return df
