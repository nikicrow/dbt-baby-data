"""Sleep-prediction modelling on top of the dbt project's `ml` schema."""

from baby_ml.compare import Comparison
from baby_ml.data import latest_snapshot, load_training_set, pull_snapshot
from baby_ml.evaluation import (
    Metrics,
    Predictions,
    metrics_table,
    plot_cumulative_recall,
    plot_feature_importance,
    plot_performance,
    plot_pr_curves,
    plot_roc_curves,
    plot_shap_summary,
    recall_at_percentiles,
)
from baby_ml.features import FeatureSpec
from baby_ml.models import MODEL_KINDS, TrainedModel, train
from baby_ml.settings import DatabaseSettings

__all__ = [
    "MODEL_KINDS",
    "Comparison",
    "DatabaseSettings",
    "FeatureSpec",
    "Metrics",
    "Predictions",
    "TrainedModel",
    "latest_snapshot",
    "load_training_set",
    "metrics_table",
    "plot_cumulative_recall",
    "plot_feature_importance",
    "plot_performance",
    "plot_pr_curves",
    "plot_roc_curves",
    "plot_shap_summary",
    "pull_snapshot",
    "recall_at_percentiles",
    "train",
]
