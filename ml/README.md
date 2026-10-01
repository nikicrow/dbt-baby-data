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

## Comparing against Jev

[Jev](https://docs.typesafe.ai/introduction) is a zero-shot model: it isn't
trained on our data. `baby_ml.jev` turns each row into a plain-English
description of the baby's state, asks one yes/no question per label, and
returns the same `Predictions` the trees produce.

Put your key in `ml/.env`, which is gitignored:

```
TYPESAFE_API_KEY=...
```

```python
from baby_ml import Comparison, JevModel

jev_preds = await JevModel().predict(df, "val")   # top-level await in Jupyter
Comparison.run(df).with_predictions(list(jev_preds.values())).metrics()
```

Answers are cached per row in `ml/data/jev/`, keyed by the model, the questions
and the state wording, so reruns are free and an interrupted run resumes. A full
`val` run costs about $0.10–0.12 per layout. See `notebooks/02_jev_comparison.ipynb`.

How a row is written for Jev is pluggable: `baby_ml/layouts.py` has four
`StateLayout`s (`narrative`, `numeric`, `minimal`, `qualitative`), chosen with
`JevModel(spec=JevSpec(layout="minimal"))`. `notebooks/03_jev_layouts.ipynb`
compares all four against the trees.

## Comparing against Kumo Tabular

[Kumo Tabular](https://huggingface.co/nvidia/Kumo-Tabular) is NVIDIA's tabular
foundation model. It learns in context: it's handed the labelled `train` rows
together with the rows to score, and predicts them in one forward pass, with
no training. It runs locally and sees the same feature columns as the trees.
It needs torch, so it's a separate opt-in group:

```bash
uv sync --group ml --group kumo
```

```python
from baby_ml.kumo import KumoModel, KumoSpec

kumo = KumoModel(spec=KumoSpec(size="small"))           # context = train
preds = kumo.predict(df, "val")                          # {label: Predictions}
Comparison.run(df).with_predictions(list(preds.values())).metrics()

KumoModel(spec=KumoSpec(context_splits=("train", "val"))).predict(df, "test")
```

The weights download from Hugging Face on first use (110 MB for `small`). On a
laptop CPU one ensemble member takes about 5 minutes per label and peaks at
about 4 GB of RAM, so fill the cache from the command line before opening the
notebook. Each label runs in its own process:

```bash
uv run python -m baby_ml.kumo val
uv run python -m baby_ml.kumo test
uv run python -m baby_ml.kumo test --context train val
```

Predictions are cached in `ml/data/kumo/`, keyed by spec, snapshot, label and
split. See `notebooks/05_kumo_comparison.ipynb`.

## Notebooks

```bash
uv run --group ml jupyter lab ml/notebooks
```

In VS Code, pick the repo's `.venv` as the kernel.
