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

## Training with 19th

The [19th Training API](https://19th.com/docs/training) workflow trains two
`event_probability` models, one for sleep **onset** within 30 minutes and one
within 60 minutes. These are probabilities for a baby who is currently awake.
Both use existing observed labels; no automatic labeling is requested.

Add `TRAINING_API_KEY=...` to `ml/.env` (gitignored). The Training key needs
`training:write`, `training:read`, and `inference:invoke` for evaluation.

From the repository root:

```bash
uv run --group ml python -m baby_ml.nineteenth prepare
uv run --group ml python -m baby_ml.nineteenth fit
# Repeat fit to read progress; it resumes the same runs.
uv run --group ml python -m baby_ml.nineteenth fit
uv run --group ml python -m baby_ml.nineteenth evaluate
# Reserve test evaluation until model choices are fixed:
uv run --group ml python -m baby_ml.nineteenth evaluate --split test
```

`prepare` is local only. It freezes the latest local parquet snapshot and saves
CSVs, exact requests, checksums and idempotency keys under
`ml/data/19th/sleep-v1/`. Use `--snapshot PATH` to pin another snapshot and
`--output PATH` for a new experiment; existing directories are never overwritten.
Use `--mode high` or `--mode ultra` on prepare for more training effort; the
default is `standard`, with the `fast` model variant. Training and inference
may incur service charges. `fit` uploads training features and one label to 19th.
Names, IDs, dates, other labels and future-derived sleep times are excluded.
Evaluation sends only the holdout feature inputs.

The chronological splits are per baby. Training labels within 60 minutes of
validation, and validation labels within 60 minutes of test, are purged.
Held-out predictions are cached in batches; metrics report PR-AUC, ROC-AUC
and Brier score alongside base rates, using the existing evaluation interface.
The consistency report counts cases where independent models produce P(30) >
P(60); probabilities are not silently adjusted. Select any yes/no threshold on
validation, based on the desired false-alarm rate, before evaluating test.

Keep the complete run folder to resume after interruption. On a lost submission
response, rerun `fit` with the same folder within seven days (the API's
idempotency lifetime). After seven days, recover the run ID from 19th rather
than replaying a submission with no saved response. HTTP failures stop the
command; respect any Retry-After before retrying. Model IDs, download readiness,
and hosted prediction URLs are saved in each `fit.json`. Download and inference
readiness are separate. This workflow uses hosted inference; it does not install
the local model runtime or change the app's live predictions.

The current sampling excludes long tracking gaps and requires a later recorded
sleep. Offline metrics therefore describe this observed cohort; validate on
fresh live data before using them as a general measure of app performance.

## Notebooks

`notebooks/06_19th_comparison.ipynb` compares the trained 19th models with
all three trees, four cached Jev layouts and cached Kumo small. It includes
ranking curves, calibration, paired bootstrap intervals by baby-day,
subgroup results, threshold tradeoffs and horizon consistency. Run All makes
no API calls and does not run Kumo inference; it refits the tree baselines
locally. Missing or mismatched prediction caches fail explicitly. The pinned
snapshot and saved 19th run under `ml/data/19th/sleep-v1/` are required.
Executed outputs are included; PNG charts and CSV tables are also exported to
that run's `analysis/` directory. The notebook explains cloud training versus
local inference, and separates documented 19th behavior from unknown internals.

```bash
uv run --group ml jupyter lab ml/notebooks
```

In VS Code, pick the repo's `.venv` as the kernel.
