"""`FeatureSpec` — which columns a model sees, as data rather than memory.

Features are everything in the training set that isn't identity, label or
split. That's derived from the DataFrame rather than listed here, so a new
feature added in dbt shows up in every model without touching Python. The
things that *are* choices — which label, which split, whether `baby_name` is a
feature, what to drop — are fields, so an experiment is fully described by its
spec and two runs can be told apart by comparing specs.
"""

from typing import ClassVar, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict

Label = Literal["is_asleep_30_mins", "is_asleep_60_mins"]
SplitColumn = Literal["split_forward_time", "split_random_day"]
SplitName = Literal["train", "val", "test"]


class FeatureSpec(BaseModel):
    """What to predict, how to split, and which columns are features.

    >>> spec = FeatureSpec()                                  # the defaults
    >>> spec = FeatureSpec(include_baby_name=True)            # §1.4 experiment
    >>> spec = FeatureSpec(exclude={"age_weeks", "age_months"})
    >>> X_train, y_train = spec.xy(df, "train")
    """

    model_config = ConfigDict(frozen=True)

    label: Label = "is_asleep_30_mins"
    split_column: SplitColumn = "split_forward_time"
    include_baby_name: bool = False
    exclude: frozenset[str] = frozenset()

    # Never features. calendar_date is here on purpose (plan §1.4): with a
    # forward-in-time split the test dates are ones the model never saw.
    IDENTITY: ClassVar[tuple[str, ...]] = (
        "prediction_id",
        "baby_id",
        "baby_name",
        "prediction_time",
        "calendar_date",
        "sample_bucket",
    )
    # All labels, not just the one being predicted: minutes_to_next_sleep
    # *is* the answer, and is_asleep_60_mins nearly is.
    LABELS: ClassVar[tuple[str, ...]] = (
        "is_asleep_30_mins",
        "is_asleep_60_mins",
        "minutes_to_next_sleep",
    )
    SPLITS: ClassVar[tuple[str, ...]] = ("split_forward_time", "split_random_day")

    def feature_columns(self, df: pd.DataFrame) -> list[str]:
        reserved = set(self.IDENTITY) | set(self.LABELS) | set(self.SPLITS) | self.exclude
        unknown = self.exclude - set(df.columns)
        if unknown:
            raise ValueError(f"exclude names columns not in the data: {sorted(unknown)}")
        features = [c for c in df.columns if c not in reserved]
        if self.include_baby_name:
            features.append("baby_name")
        return features

    def categorical_columns(self, df: pd.DataFrame) -> list[str]:
        return [
            c for c in self.feature_columns(df) if isinstance(df[c].dtype, pd.CategoricalDtype)
        ]

    def xy(self, df: pd.DataFrame, split: SplitName) -> tuple[pd.DataFrame, pd.Series]:
        """Features and label for one split, e.g. `spec.xy(df, "val")`."""
        rows = df[self.split_column] == split
        return df.loc[rows, self.feature_columns(df)], df.loc[rows, self.label]
