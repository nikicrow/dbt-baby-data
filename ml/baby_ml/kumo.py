"""Score a split with NVIDIA's Kumo Tabular and get back `Predictions`.

Kumo Tabular (huggingface.co/nvidia/Kumo-Tabular) is a tabular foundation
model that learns *in context*: nothing is trained. It's handed labelled rows
(the "context", here the `train` split) together with the rows to score, and
predicts their labels in one forward pass, the way an LLM answers from
examples in its prompt. It sees exactly the columns the trees see (the same
`FeatureSpec`), so a difference in the numbers is the model, not the features.

Unlike Jev, a row's answer depends on which rows are in the context, so the
context is part of the experiment: `KumoSpec.context_splits=("train", "val")`
is the counterpart of refitting XGBoost on train + val.

The pieces:

- **`KumoSpec`** is *what* to run: model size, ensemble size, seed, and which
  splits form the context. Frozen, because (with the snapshot name) its hash
  names the cache file.
- **`KumoModel`** is *how*: device and query batch size, which don't change
  the answers. `predict` returns one `Predictions` per label.

Usage, in a notebook:

    from baby_ml.kumo import KumoModel, KumoSpec

    kumo = KumoModel(spec=KumoSpec(size="small"))
    preds = kumo.predict(df, "val")                      # {label: Predictions}
    Comparison.run(df).with_predictions(list(preds.values()))

Needs the opt-in dependency group: `uv sync --group ml --group kumo`. The
weights (110 MB for small) download from Hugging Face on first use.

It's slow on a laptop CPU: with the small model, one ensemble member takes
about 5 minutes per label with ~20k context rows, peaking at ~4 GB of RAM. So
predictions are cached per (spec, snapshot, label, split) in `ml/data/kumo/`,
a rerun is instant, and `python -m baby_ml.kumo` (see `main`) fills the cache
outside a notebook.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import torch
from pydantic import BaseModel, ConfigDict, PrivateAttr

# The Hugging Face cache warns on every download that Windows can't symlink
# without Developer Mode. It copies instead, which is fine for us.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import sdm  # noqa: E402  (after the env var, which huggingface_hub reads on import)

from baby_ml.data import SNAPSHOT_DIR  # noqa: E402
from baby_ml.evaluation import Predictions  # noqa: E402
from baby_ml.features import FeatureSpec, Label, SplitColumn, SplitName  # noqa: E402

CACHE_DIR = SNAPSHOT_DIR / "kumo"
LABELS: tuple[Label, ...] = ("is_asleep_30_mins", "is_asleep_60_mins")

KumoSize = Literal["small", "medium", "large"]


class KumoSpec(BaseModel):
    """Everything that decides Kumo's answers, besides the rows themselves."""

    model_config = ConfigDict(frozen=True)

    size: KumoSize = "small"
    # Each member sees a differently preprocessed copy of the table (column
    # order, transforms, class order) and their probabilities are averaged.
    # Cost is linear in this. NVIDIA's examples use 8 on a GPU, but on the
    # 16 GB laptop 4 members over ~20k context rows crashed the process
    # (access violation mid-fit) while 1 runs fine, so 1 is the default.
    num_estimators: int = 1
    seed: int = 0
    split_column: SplitColumn = "split_forward_time"
    context_splits: tuple[SplitName, ...] = ("train",)
    include_baby_name: bool = False
    # Weights tag on the Hub, as loaded by structured-data-models. Recorded
    # here so a new release can't silently mix into an old cache.
    weights_revision: str = "v1.0.0"

    def feature_spec(self, label: Label) -> FeatureSpec:
        return FeatureSpec(
            label=label, split_column=self.split_column, include_baby_name=self.include_baby_name
        )

    def cache_key(self, snapshot: str, label: Label, split: SplitName) -> str:
        payload = {**self.model_dump(), "snapshot": snapshot, "label": label, "split": split}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


class KumoModel(BaseModel):
    """Scores a split with Kumo Tabular, one in-context run per label.

    >>> kumo = KumoModel()
    >>> preds = kumo.predict(df, "val")
    >>> metrics_table(list(preds.values()))
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    spec: KumoSpec = KumoSpec()
    device: str = "cuda" if torch.cuda.is_available() else "cpu"
    # Query rows scored per call. Queries only attend to the (cached)
    # context, so batching changes memory use, never the answers.
    query_batch_size: int = 1000
    _model: sdm.models.KumoTabular | None = PrivateAttr(default=None)

    @property
    def name(self) -> str:
        suffix = "_" + "_".join(self.spec.context_splits) if self.spec.context_splits != ("train",) else ""
        return f"kumo_{self.spec.size}{suffix}"

    def cache_path(self, df: pd.DataFrame, label: Label, split: SplitName) -> Path:
        return CACHE_DIR / f"{self.spec.cache_key(df.attrs['snapshot'], label, split)}.parquet"

    def predict(
        self, df: pd.DataFrame, split: SplitName = "val", labels: tuple[Label, ...] = LABELS
    ) -> dict[Label, Predictions]:
        """Kumo's probabilities for every row of `split`, one `Predictions` per label."""
        if split in self.spec.context_splits:
            raise ValueError(f"{split} is in the context, so scoring it would be leakage")
        rows = df[df[self.spec.split_column] == split]
        return {
            label: Predictions(
                name=self.name,
                label=label,
                split=split,
                y_true=rows[label].to_numpy(),
                p=self._scores(df, label, split).loc[rows["prediction_id"]].to_numpy(dtype=float),
            )
            for label in labels
        }

    def _scores(self, df: pd.DataFrame, label: Label, split: SplitName) -> pd.Series:
        """P(label = 1) per prediction_id, from the cache or a fresh run."""
        path = self.cache_path(df, label, split)
        if path.exists():
            return pd.read_parquet(path).set_index("prediction_id")["p"]

        feature_spec = self.spec.feature_spec(label)
        in_context = df[self.spec.split_column].isin(self.spec.context_splits)
        context = df[in_context]
        query = df[df[self.spec.split_column] == split]
        columns = feature_spec.feature_columns(df)

        print(
            f"Kumo {self.spec.size} x{self.spec.num_estimators} on {label}: "
            f"{len(context):,} context rows, {len(query):,} to score ({self.device})"
        )
        start = time.perf_counter()
        # Context and query go through one TableTensor so both get the same
        # column types and category vocabularies.
        x = _table(pd.concat([context[columns], query[columns]]), self.device)
        y = sdm.TableTensor.from_pandas(
            pd.DataFrame({"y": context[label].astype(str).to_numpy()}),
            stypes={"y": "categorical"},
            device=self.device,
        )
        n = len(context)
        model = self._load()
        generator = torch.Generator(device=self.device).manual_seed(self.spec.seed)
        with torch.inference_mode(), self._autocast():
            model.fit(x=x[:n], y=y, num_estimators=self.spec.num_estimators, generator=generator)
            print(f"  context encoded in {time.perf_counter() - start:.0f}s")
            try:
                p = np.concatenate(
                    [
                        _positive_class(model.predict(x[i : i + self.query_batch_size]))
                        for i in range(n, len(x), self.query_batch_size)
                    ]
                )
            finally:
                model.clear()
        print(f"  done in {time.perf_counter() - start:.0f}s")

        scores = pd.DataFrame({"prediction_id": query["prediction_id"].to_numpy(), "p": p})
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        scores.to_parquet(path, index=False)
        return scores.set_index("prediction_id")["p"]

    def _load(self) -> sdm.models.KumoTabular:
        if self._model is None:
            self._model = sdm.models.KumoTabular(
                task="classification", size=self.spec.size, device=self.device
            )
        return self._model

    def _autocast(self):
        # Half precision on GPU, as NVIDIA's examples do. On CPU it's slower,
        # so stay in float32.
        return torch.amp.autocast(self.device, torch.float16, enabled=self.device == "cuda")


def main() -> None:
    """Fill the cache from the command line, one label per process.

    On CPU, one ensemble member peaks at about 4 GB with ~20k context rows
    (sdm only chunks the work on CUDA), which can kill a notebook kernel
    that's already holding other models. A fresh process per label gives
    that memory back between runs. The notebook then reads the cache.

        uv run python -m baby_ml.kumo val
        uv run python -m baby_ml.kumo test --context train val
    """
    import argparse  # noqa: PLC0415

    from baby_ml.data import load_training_set  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=main.__doc__.splitlines()[0])
    parser.add_argument("split", choices=["val", "test"])
    parser.add_argument("--context", nargs="+", default=["train"], choices=["train", "val"])
    parser.add_argument("--label", choices=LABELS, help="default: every label")
    parser.add_argument("--size", default="small", choices=["small", "medium", "large"])
    parser.add_argument("--estimators", type=int, default=KumoSpec().num_estimators)
    args = parser.parse_args()

    spec = KumoSpec(size=args.size, num_estimators=args.estimators, context_splits=tuple(args.context))
    model = KumoModel(spec=spec)
    df = load_training_set()
    labels = [args.label] if args.label else list(LABELS)
    for label in labels:
        if model.cache_path(df, label, args.split).exists():
            print(f"{model.name} {label} on {args.split}: cached")
        elif len(labels) > 1:
            # A child process per label, so each starts with free memory.
            import subprocess  # noqa: PLC0415
            import sys  # noqa: PLC0415

            subprocess.run([sys.executable, "-m", "baby_ml.kumo", *sys.argv[1:], "--label", label], check=True)
        else:
            model.predict(df, args.split, labels=(label,))


def _table(X: pd.DataFrame, device: str) -> sdm.TableTensor:
    # Booleans become 0/1 numbers rather than two-level categories: the same
    # information, and the numerical path skips category bookkeeping.
    X = X.astype({c: "int8" for c in X.columns if X[c].dtype == bool})
    return sdm.TableTensor.from_pandas(X, stypes=sdm.infer_stypes(X), device=device)


def _positive_class(out: sdm.TableTensor) -> np.ndarray:
    # Pick the column by name: the class order changes from call to call.
    classes = list(out.columns[sdm.Stype.numerical])
    return out.numerical[:, classes.index("1")].float().cpu().numpy()


if __name__ == "__main__":
    main()
