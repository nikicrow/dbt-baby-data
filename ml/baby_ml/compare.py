"""Train every model kind on every label and put them side by side.

>>> comparison = Comparison.run(df)            # 3 models x 2 labels, scored on val
>>> comparison.metrics()
>>> comparison.plot_performance()
>>> comparison.shap_importance()

All models share one `FeatureSpec` per label (same features, same split), so
any difference in the numbers is the model, not the data it saw.
"""

from collections.abc import Sequence
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from pydantic import BaseModel, ConfigDict, PrivateAttr

from baby_ml.evaluation import (
    Predictions,
    mean_abs_shap,
    metrics_table,
    plot_cumulative_recall,
    plot_pr_curves,
    plot_roc_curves,
    plot_shap_beeswarm,
    recall_at_percentiles,
)
from baby_ml.features import FeatureSpec, Label, SplitName
from baby_ml.models import MODEL_KINDS, ModelKind, TrainedModel, train

LABELS: tuple[Label, ...] = ("is_asleep_30_mins", "is_asleep_60_mins")


class Comparison(BaseModel):
    """Fitted models and their predictions, keyed by (label, model kind)."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    df: pd.DataFrame
    split: SplitName
    models: dict[tuple[Label, ModelKind], TrainedModel]
    predictions: dict[tuple[Label, ModelKind], Predictions]
    shap_rows: int | None = 1000  # rows sampled for SHAP; None explains them all
    # SHAP on the forest is slow, so compute each model's values once.
    _shap: dict[tuple[Label, ModelKind], tuple[np.ndarray, pd.DataFrame]] = PrivateAttr(
        default_factory=dict
    )

    @classmethod
    def run(
        cls,
        df: pd.DataFrame,
        labels: Sequence[Label] = LABELS,
        kinds: Sequence[ModelKind] = MODEL_KINDS,
        split: SplitName = "val",
        base_spec: FeatureSpec | None = None,
        params: dict[ModelKind, dict[str, Any]] | None = None,
    ) -> "Comparison":
        """Train each kind on each label, then score them all on `split`.

        `base_spec` sets everything but the label (split column, exclusions,
        ...); `params` overrides hyperparameters per kind.
        """
        base_spec = base_spec or FeatureSpec()
        params = params or {}
        models, predictions = {}, {}
        for label in labels:
            spec = base_spec.model_copy(update={"label": label})
            for kind in kinds:
                model = train(kind, df, spec, params.get(kind))
                models[(label, kind)] = model
                predictions[(label, kind)] = Predictions.from_model(model, df, split)
        return cls(df=df, split=split, models=models, predictions=predictions)

    @property
    def labels(self) -> list[Label]:
        return list(dict.fromkeys(label for label, _ in self.models))

    @property
    def kinds(self) -> list[ModelKind]:
        return list(dict.fromkeys(kind for _, kind in self.models))

    def predictions_for(self, label: Label) -> list[Predictions]:
        return [self.predictions[(label, kind)] for kind in self.kinds]

    def metrics(self) -> pd.DataFrame:
        return metrics_table(list(self.predictions.values()))

    def recall_at_percentiles(self, percentiles: Sequence[float] = (5, 10, 20, 30, 50)) -> pd.DataFrame:
        return recall_at_percentiles(list(self.predictions.values()), percentiles)

    def plot_performance(self) -> Figure:
        """One row per label: PR, ROC and cumulative recall, all models overlaid."""
        fig, axes = plt.subplots(len(self.labels), 3, figsize=(15, 4.5 * len(self.labels)), squeeze=False)
        for row, label in zip(axes, self.labels):
            preds = self.predictions_for(label)
            plot_pr_curves(preds, row[0])
            plot_roc_curves(preds, row[1])
            plot_cumulative_recall(preds, row[2])
            for ax in row:
                ax.set_title(f"{ax.get_title()} — {label}")
        fig.suptitle(f"Model comparison on {self.split}")
        fig.tight_layout()
        return fig

    def shap_values(self, label: Label, kind: ModelKind) -> tuple[np.ndarray, pd.DataFrame]:
        """One model's SHAP values on the comparison split (cached per model)."""
        key = (label, kind)
        if key not in self._shap:
            model = self.models[key]
            X, _ = model.spec.xy(self.df, self.split)
            self._shap[key] = model.shap_values(X, max_rows=self.shap_rows)
        return self._shap[key]

    def shap_importance(self, top: int = 15) -> pd.DataFrame:
        """Normalised mean |SHAP| per feature, one column per (label, model).

        Keeps the union of each model's top features, ordered by their average
        share, so it shows both what the models agree on and where they differ.
        """
        columns = {}
        for label, kind in self.models:
            values, X_shown = self.shap_values(label, kind)
            columns[(label, kind)] = mean_abs_shap(values, X_shown.columns)
        table = pd.DataFrame(columns)
        table.columns.names = ["label", "model"]
        keep = set().union(*(table[c].nlargest(top).index for c in table.columns))
        return table.loc[sorted(keep, key=lambda f: -table.loc[f].mean())]

    def plot_shap_summaries(self, max_display: int = 15) -> list[Figure]:
        """A SHAP beeswarm for every (label, model), in label-then-model order."""
        figures = []
        for label, kind in self.models:
            values, X_shown = self.shap_values(label, kind)
            figures.append(plot_shap_beeswarm(values, X_shown, f"{kind} — SHAP ({label})", max_display))
        return figures
