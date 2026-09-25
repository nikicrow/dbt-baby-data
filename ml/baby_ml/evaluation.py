"""Metrics and charts that work the same for any model, or several at once.

Everything takes `Predictions` — labels and scores with a name attached —
rather than a model, so one function draws a single model's curve or overlays
three for a comparison. Feature importance and SHAP take a `TrainedModel`,
which hides the per-library differences.

Always read PR-AUC and the cumulative-recall curve against the base rate: it
drifts from ~33% in train to ~20% in test, so a raw PR-AUC isn't comparable
across splits.
"""

from collections.abc import Sequence

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import shap
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from pydantic import BaseModel, ConfigDict
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    precision_recall_curve,
    roc_auc_score,
    roc_curve,
)

from baby_ml.features import Label, SplitName
from baby_ml.models import TrainedModel

# One fixed colour per model, so a model looks the same on every chart. These
# are the first four slots of a CVD-validated categorical palette.
MODEL_COLORS = {
    "lightgbm": "#2a78d6",
    "xgboost": "#eb6834",
    "random_forest": "#1baf7a",
    "jev": "#e87ba4",
}
REFERENCE_COLOR = "#8a8984"  # chance / base-rate / perfect lines
_FALLBACK_COLORS = ("#4a3aa7", "#e34948")


class Predictions(BaseModel):
    """One model's scores on one split, ready for any metric or chart here."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    label: Label
    split: SplitName
    y_true: np.ndarray
    p: np.ndarray

    @classmethod
    def from_model(cls, model: TrainedModel, df: pd.DataFrame, split: SplitName) -> "Predictions":
        y, p = model.predict_split(df, split)
        return cls(name=model.name, label=model.spec.label, split=split, y_true=y.to_numpy(), p=p)

    @property
    def base_rate(self) -> float:
        return float(self.y_true.mean())


class Metrics(BaseModel):
    """The headline numbers for one set of predictions."""

    model: str
    label: Label
    split: SplitName
    rows: int
    base_rate: float
    pr_auc: float
    roc_auc: float
    brier: float
    brier_base_rate: float  # what always predicting the base rate would score
    mean_predicted: float  # well above base_rate means over-confident

    @classmethod
    def of(cls, pred: Predictions) -> "Metrics":
        y, p = pred.y_true, pred.p
        return cls(
            model=pred.name,
            label=pred.label,
            split=pred.split,
            rows=len(y),
            base_rate=pred.base_rate,
            pr_auc=average_precision_score(y, p),
            roc_auc=roc_auc_score(y, p),
            brier=brier_score_loss(y, p),
            brier_base_rate=brier_score_loss(y, np.full(len(y), pred.base_rate)),
            mean_predicted=float(p.mean()),
        )


def metrics_table(preds: Sequence[Predictions]) -> pd.DataFrame:
    """One row per prediction set, indexed by label and model."""
    return pd.DataFrame([Metrics.of(p).model_dump() for p in preds]).set_index(["label", "model"])


# --- cumulative recall -------------------------------------------------------


def cumulative_recall(pred: Predictions) -> pd.DataFrame:
    """Share of all positives caught by flagging the top x% of predictions.

    Rows are ranked by score, highest first; `percentile` is how many of them
    are flagged and `recall` is the share of positives among those. A random
    model sits on the diagonal and a perfect one reaches 1 at the base rate.
    """
    order = np.argsort(-pred.p, kind="stable")
    hits = np.cumsum(pred.y_true[order])
    n = len(order)
    return pd.DataFrame(
        {
            "percentile": np.arange(1, n + 1) / n * 100,
            "recall": hits / hits[-1],
        }
    )


def recall_at_percentiles(
    preds: Sequence[Predictions], percentiles: Sequence[float] = (5, 10, 20, 30, 50)
) -> pd.DataFrame:
    """Recall when flagging the top x% of rows, for each prediction set."""
    rows = {}
    for pred in preds:
        curve = cumulative_recall(pred)
        rows[(pred.label, pred.name)] = {
            f"top {q:g}%": curve.loc[curve["percentile"] <= q, "recall"].max() for q in percentiles
        }
    table = pd.DataFrame(rows).T
    table.index.names = ["label", "model"]
    return table


# --- charts ------------------------------------------------------------------


def plot_pr_curves(preds: Sequence[Predictions], ax: Axes | None = None) -> Axes:
    """Precision-recall curves, with the base rate as the no-skill line."""
    ax = ax or plt.subplots(figsize=(5, 4.5))[1]
    for i, pred in enumerate(preds):
        precision, recall, _ = precision_recall_curve(pred.y_true, pred.p)
        auc = average_precision_score(pred.y_true, pred.p)
        ax.plot(recall, precision, lw=2, color=_color(pred.name, i), label=f"{pred.name} ({auc:.3f})")
    base = preds[0].base_rate
    ax.axhline(base, ls="--", lw=1, color=REFERENCE_COLOR, label=f"base rate ({base:.3f})")
    _style(ax, "Precision-recall", "recall", "precision")
    return ax


def plot_roc_curves(preds: Sequence[Predictions], ax: Axes | None = None) -> Axes:
    """ROC curves, with the diagonal as the no-skill line."""
    ax = ax or plt.subplots(figsize=(5, 4.5))[1]
    for i, pred in enumerate(preds):
        fpr, tpr, _ = roc_curve(pred.y_true, pred.p)
        auc = roc_auc_score(pred.y_true, pred.p)
        ax.plot(fpr, tpr, lw=2, color=_color(pred.name, i), label=f"{pred.name} ({auc:.3f})")
    ax.plot([0, 1], [0, 1], ls="--", lw=1, color=REFERENCE_COLOR, label="chance (0.500)")
    _style(ax, "ROC", "false positive rate", "true positive rate")
    return ax


def plot_cumulative_recall(preds: Sequence[Predictions], ax: Axes | None = None) -> Axes:
    """Recall against the share of rows flagged, best-scored first."""
    ax = ax or plt.subplots(figsize=(5, 4.5))[1]
    for i, pred in enumerate(preds):
        curve = cumulative_recall(pred)
        ax.plot(curve["percentile"], curve["recall"], lw=2, color=_color(pred.name, i), label=pred.name)
    base = preds[0].base_rate * 100
    ax.plot([0, base, 100], [0, 1, 1], ls=":", lw=1, color=REFERENCE_COLOR, label="perfect")
    ax.plot([0, 100], [0, 1], ls="--", lw=1, color=REFERENCE_COLOR, label="random")
    _style(ax, "Cumulative recall", "top % of rows by predicted probability", "share of positives caught")
    ax.set_xlim(0, 100)
    return ax


def plot_performance(preds: Sequence[Predictions], title: str | None = None) -> Figure:
    """PR, ROC and cumulative-recall side by side, for one model or several."""
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    plot_pr_curves(preds, axes[0])
    plot_roc_curves(preds, axes[1])
    plot_cumulative_recall(preds, axes[2])
    if title is None:
        title = f"{preds[0].label} — {preds[0].split}"
    fig.suptitle(title)
    fig.tight_layout()
    return fig


def plot_feature_importance(model: TrainedModel, top: int = 15, ax: Axes | None = None) -> Axes:
    """The library's native importance (gain / impurity), top features only."""
    ax = ax or plt.subplots(figsize=(6, 0.3 * top + 1))[1]
    model.feature_importance().sort_values().tail(top).plot.barh(ax=ax, color=_color(model.name, 0), width=0.7)
    ax.set(title=f"{model.name} — top {top} by native importance", xlabel="share of total importance")
    ax.grid(axis="x", alpha=0.3)
    ax.spines[["top", "right"]].set_visible(False)
    return ax


def plot_shap_summary(
    model: TrainedModel, X: pd.DataFrame, max_display: int = 20, max_rows: int | None = 1000
) -> Figure:
    """SHAP beeswarm: each dot is one row, placed by its effect on the prediction.

    The boosters' effects are in log-odds and the forest's in probability, so
    compare models by the *ordering* of features, not the x-axis scale.
    """
    values, X_shown = model.shap_values(X, max_rows=max_rows)
    return plot_shap_beeswarm(values, X_shown, f"{model.name} — SHAP ({model.spec.label})", max_display)


def plot_shap_beeswarm(
    values: np.ndarray, X_shown: pd.DataFrame, title: str, max_display: int = 20
) -> Figure:
    """The beeswarm for already-computed SHAP values (see `TrainedModel.shap_values`)."""
    # summary_plot draws into the current figure, so give it a fresh one.
    plt.figure()
    shap.summary_plot(values, X_shown, max_display=max_display, show=False)
    fig = plt.gcf()
    fig.suptitle(title)
    fig.tight_layout()
    return fig


def mean_abs_shap(values: np.ndarray, columns: Sequence[str]) -> pd.Series:
    """Mean |SHAP| per feature, normalised to sum to 1 so models compare."""
    importance = pd.Series(np.abs(values).mean(axis=0), index=list(columns))
    return importance / importance.sum()


def _color(name: str, i: int) -> str:
    return MODEL_COLORS.get(name, _FALLBACK_COLORS[i % len(_FALLBACK_COLORS)])


def _style(ax: Axes, title: str, xlabel: str, ylabel: str) -> None:
    ax.set(title=title, xlabel=xlabel, ylabel=ylabel, ylim=(0, 1.02))
    ax.grid(alpha=0.3)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(loc="best", fontsize=8, frameon=False)
