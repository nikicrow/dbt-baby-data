"""`TrainedModel` — one interface over LightGBM, XGBoost and random forest.

Everything downstream (metrics, curves, importance, SHAP) talks to a
`TrainedModel`, never to a library directly, so a new model kind only has to
be taught here. The one real difference between the three is categoricals:
the boosters take pandas `category` columns natively, while sklearn's random
forest needs numbers, so `prepare` swaps each category for its integer code
(missing stays NaN, which sklearn >= 1.4 trees split on).
"""

import warnings
from typing import Any, Literal

import lightgbm as lgb
import numpy as np
import pandas as pd
import shap
import xgboost as xgb
from pydantic import BaseModel, ConfigDict
from sklearn.ensemble import RandomForestClassifier

from baby_ml.features import FeatureSpec, SplitName

ModelKind = Literal["lightgbm", "xgboost", "random_forest"]
MODEL_KINDS: tuple[ModelKind, ...] = ("lightgbm", "xgboost", "random_forest")

# Untuned starting points, roughly comparable in capacity. Override any of
# them per run with `train(..., params={...})`.
DEFAULT_PARAMS: dict[ModelKind, dict[str, Any]] = {
    "lightgbm": {"n_estimators": 300, "learning_rate": 0.05, "num_leaves": 31, "verbose": -1},
    "xgboost": {
        "n_estimators": 300,
        "learning_rate": 0.05,
        "max_depth": 6,
        "tree_method": "hist",
        "enable_categorical": True,
    },
    "random_forest": {"n_estimators": 300, "min_samples_leaf": 20, "n_jobs": -1},
}


class TrainedModel(BaseModel):
    """A fitted classifier plus the spec that says which columns it saw."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    kind: ModelKind
    spec: FeatureSpec
    estimator: Any
    feature_columns: list[str]
    categorical_columns: list[str]

    @property
    def name(self) -> str:
        return self.kind

    def prepare(self, X: pd.DataFrame) -> pd.DataFrame:
        """Put features into the form this model's library accepts."""
        X = X[self.feature_columns]
        if self.kind != "random_forest" or not self.categorical_columns:
            return X
        X = X.copy()
        for column in self.categorical_columns:
            X[column] = X[column].cat.codes.replace(-1, np.nan).astype(float)
        return X

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """P(label = 1) for each row."""
        return self.estimator.predict_proba(self.prepare(X))[:, 1]

    def predict_split(self, df: pd.DataFrame, split: SplitName) -> tuple[pd.Series, np.ndarray]:
        """Labels and predictions for one split, e.g. `model.predict_split(df, "val")`."""
        X, y = self.spec.xy(df, split)
        return y, self.predict_proba(X)

    def feature_importance(self) -> pd.Series:
        """The library's own importance, normalised to sum to 1.

        Gain for the boosters, mean impurity decrease for the forest. Both
        measure how much a feature's splits improved the training loss, so they
        rank comparably — but they're biased towards high-cardinality features,
        so read them next to SHAP rather than instead of it.
        """
        if self.kind == "lightgbm":
            raw = self.estimator.booster_.feature_importance("gain")
        elif self.kind == "xgboost":
            scores = self.estimator.get_booster().get_score(importance_type="total_gain")
            raw = [scores.get(c, 0.0) for c in self.feature_columns]
        else:
            raw = self.estimator.feature_importances_
        importance = pd.Series(raw, index=self.feature_columns, dtype=float)
        return importance / importance.sum()

    def shap_values(
        self, X: pd.DataFrame, max_rows: int | None = 1000, seed: int = 0
    ) -> tuple[np.ndarray, pd.DataFrame]:
        """SHAP values for the positive class, and the rows they explain.

        Rows are sampled down to `max_rows` because exact TreeSHAP on a random
        forest's deep trees is slow (tens of seconds per thousand rows); the
        boosters would cope with the lot. The returned frame is the prepared
        (numeric) one, which is what SHAP's plots need to colour by value.
        """
        if max_rows is not None and len(X) > max_rows:
            X = X.sample(max_rows, random_state=seed)
        X = self.prepare(X)
        with warnings.catch_warnings():
            # LightGBM's "output has changed to a list" notice; handled below.
            warnings.filterwarnings("ignore", message=".*output has changed to a list")
            values = shap.TreeExplainer(self.estimator).shap_values(X)
        # One set per class: a [neg, pos] list from LightGBM, (rows, features,
        # 2) from the forest. XGBoost gives the positive class directly.
        if isinstance(values, list):
            values = values[1]
        elif values.ndim == 3:
            values = values[:, :, 1]
        return values, _numeric_for_display(X)


def train(
    kind: ModelKind,
    df: pd.DataFrame,
    spec: FeatureSpec | None = None,
    params: dict[str, Any] | None = None,
    seed: int = 0,
) -> TrainedModel:
    """Fit one model kind on the spec's train split."""
    spec = spec or FeatureSpec()
    params = {**DEFAULT_PARAMS[kind], "random_state": seed, **(params or {})}
    features = spec.feature_columns(df)
    model = TrainedModel(
        kind=kind,
        spec=spec,
        estimator=_estimator(kind, params),
        feature_columns=features,
        categorical_columns=spec.categorical_columns(df),
    )
    X, y = spec.xy(df, "train")
    model.estimator.fit(model.prepare(X), y)
    return model


def _estimator(kind: ModelKind, params: dict[str, Any]) -> Any:
    if kind == "lightgbm":
        return lgb.LGBMClassifier(**params)
    if kind == "xgboost":
        return xgb.XGBClassifier(**params)
    return RandomForestClassifier(**params)


def _numeric_for_display(X: pd.DataFrame) -> pd.DataFrame:
    """SHAP's beeswarm colours points by feature value, which needs numbers."""
    X = X.copy()
    for column in X.columns:
        if isinstance(X[column].dtype, pd.CategoricalDtype):
            X[column] = X[column].cat.codes.replace(-1, np.nan).astype(float)
        elif X[column].dtype == bool:
            X[column] = X[column].astype(int)
    return X
