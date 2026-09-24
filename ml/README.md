# baby-ml

Sleep-prediction models trained on `ml.ml_sleep_training_set`, which the dbt
project in `baby_data/` builds. This is its own package, outside the dbt project;
see `claude_plans/ML_SLEEP_PREDICTION_PLAN.md` for the data design.

## Setup

```bash
uv sync --group ml
```

This installs `baby_ml` (editable) plus Jupyter. A plain `uv sync`, which CI
runs, leaves it all out.

## Getting data

Open the tunnel first, since the database is on fedora-1:

```bash
ssh -N fedora-1-db
```

Credentials come from the `fedora_readonly` target in `~/.dbt/profiles.yml`,
so there's nothing else to configure. To override them, copy `.env.example` to
`ml/.env`.

```python
from baby_ml import FeatureSpec, load_training_set

df = load_training_set()              # newest snapshot in ml/data/, pulling one if none
df = load_training_set(refresh=True)  # pull a fresh snapshot from the database
X_train, y_train = FeatureSpec().xy(df, "train")
```

Snapshots are timestamped parquet files in `ml/data/` (gitignored), and are
never overwritten.

## Models and evaluation

LightGBM, XGBoost and random forest all go through one interface, so every
metric and chart works for any of them, or for several overlaid:

```python
from baby_ml import Comparison, Predictions, plot_performance, plot_shap_summary, train

model = train("xgboost", df)                  # or "lightgbm", "random_forest"
pred = Predictions.from_model(model, df, "val")
plot_performance([pred])                      # PR, ROC and cumulative-recall curves
plot_shap_summary(model, X_val)

comparison = Comparison.run(df)               # all 3 models x both labels, scored on val
comparison.metrics()                          # PR-AUC, ROC-AUC, Brier, next to the base rate
comparison.plot_performance()
comparison.recall_at_percentiles()            # share of sleeps caught in the top x%
comparison.shap_importance()                  # mean |SHAP| per feature, per model
```

## Notebooks

```bash
uv run --group ml jupyter lab ml/notebooks
```

In VS Code, pick the repo's `.venv` as the kernel.
