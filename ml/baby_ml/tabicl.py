"""Local TabICL inference, pinned weights, and content-addressed predictions.

Run ``python -m baby_ml.tabicl download`` or ``python -m baby_ml.tabicl val``.
Optional dependencies: ``uv sync --group ml --group tabicl``.
"""

import argparse
import hashlib
import json
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from baby_ml.data import SNAPSHOT_DIR, load_training_set
from baby_ml.evaluation import Predictions, metrics_table
from baby_ml.features import FeatureSpec, Label, SplitName

LABELS: tuple[Label, ...] = ("is_asleep_30_mins", "is_asleep_60_mins")
REPO = "jingang/TabICL"
REVISION = "4dcd344ece2c00be9e831fdd35bed57b5ad83e19"
CHECKPOINT = "tabicl-classifier-v2-20260212.ckpt"


class TabICLSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    features: FeatureSpec = FeatureSpec()
    context_splits: tuple[SplitName, ...] = ("train",)
    n_estimators: int = Field(default=1, ge=1)
    seed: int = 0
    device: str = "cpu"


def download_model() -> Path:
    """Download only the pinned classifier (~110 MB); never upload baby data."""
    from huggingface_hub import hf_hub_download

    return Path(hf_hub_download(
        repo_id=REPO, filename=CHECKPOINT, revision=REVISION,
        cache_dir=SNAPSHOT_DIR / "tabicl" / "hub",
    ))


class TabICLModel:
    def __init__(self, spec: TabICLSpec | None = None):
        self.spec = spec or TabICLSpec()

    @property
    def name(self) -> str:
        suffix = "" if self.spec.context_splits == ("train",) else "_" + "_".join(self.spec.context_splits)
        return "tabicl" + suffix

    def _rows(self, df, split):
        allowed = {"val": {"train"}, "test": {"train", "val"}}
        if not self.spec.context_splits or not set(self.spec.context_splits) <= allowed.get(split, set()):
            raise ValueError("Use train context for val; train and/or val context for test.")
        col = self.spec.features.split_column
        context = df.loc[df[col].isin(self.spec.context_splits)]
        query = df.loc[df[col] == split]
        if context.empty or query.empty:
            raise ValueError("Context and query must both contain rows.")
        if df.prediction_id.isna().any() or not df.prediction_id.is_unique:
            raise ValueError("prediction_id must be present and unique.")
        return context, query

    def cache_path(self, df, label, split):
        context, query = self._rows(df, split)
        columns = self.spec.features.feature_columns(df)
        payload = {
            "spec": self.spec.model_dump(mode="json"), "revision": REVISION,
            "checkpoint": CHECKPOINT, "label": label, "split": split,
            "columns": columns, "schema": [str(df[c].dtype) for c in columns],
            "versions": {p: version(p) for p in ("tabicl", "torch", "scikit-learn")},
            "format": 1,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode())
        # Hash actual ordered inputs, not just the snapshot filename. A subset,
        # changed label, feature edit, or reordered query cannot reuse stale scores.
        for rows in (context, query):
            digest.update(pd.util.hash_pandas_object(
                rows[["prediction_id", *columns, label]], index=False
            ).to_numpy().tobytes())
        return SNAPSHOT_DIR / "tabicl" / f"{digest.hexdigest()}.parquet"

    def fit(self, df, label: Label, split: SplitName = "val"):
        """Return a sklearn classifier for hands-on predict_proba calls.

        fit supplies labelled context; it does not update pretrained weights.
        Preprocessing is fitted by TabICL on context only.
        """
        from tabicl import TabICLClassifier

        context, _ = self._rows(df, split)
        if set(context[label].dropna().unique()) != {0, 1} or context[label].isna().any():
            raise ValueError(f"Context for {label} must contain both binary classes and no missing labels.")
        model = TabICLClassifier(
            model_path=download_model(), allow_auto_download=False,
            checkpoint_version=CHECKPOINT, n_estimators=self.spec.n_estimators,
            random_state=self.spec.seed, device=self.spec.device, n_jobs=1,
        )
        model.fit(context[self.spec.features.feature_columns(df)], context[label].astype(int))
        return model

    def predict(self, df, split: SplitName = "val", labels=LABELS, *, cache_only=False):
        _, query = self._rows(df, split)
        predictions = {}
        for label in labels:
            if label not in LABELS:
                raise ValueError(f"Unknown label: {label}")
            path = self.cache_path(df, label, split)
            if path.exists():
                scores = pd.read_parquet(path).set_index("prediction_id")
                p = scores.loc[query.prediction_id, "p"].to_numpy()
            else:
                if cache_only:
                    raise FileNotFoundError(f"No cached {label}/{split}; run python -m baby_ml.tabicl {split}")
                print(f"{self.name}: {label}, {len(query):,} queries on {self.spec.device}", flush=True)
                model = self.fit(df, label, split)
                p = model.predict_proba(query[self.spec.features.feature_columns(df)])[:, list(model.classes_).index(1)]
                del model
                if not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
                    raise ValueError("Invalid model probabilities")
                path.parent.mkdir(parents=True, exist_ok=True)
                pd.DataFrame({"prediction_id": query.prediction_id.to_numpy(), "p": p}).to_parquet(path, index=False)
            predictions[label] = Predictions(name=self.name, label=label, split=split,
                                             y_true=query[label].to_numpy(), p=p)
        return predictions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["download", "val", "test"])
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--context", nargs="+", choices=["train", "val"], default=["train"])
    parser.add_argument("--estimators", type=int, default=1)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--label", choices=LABELS)
    args = parser.parse_args()
    if args.action == "download":
        print(download_model())
        return
    model = TabICLModel(TabICLSpec(context_splits=tuple(args.context),
                                  n_estimators=args.estimators, device=args.device))
    df = load_training_set(path=args.snapshot)
    print("Snapshot:", df.attrs["snapshot"])
    preds = model.predict(df, args.action, labels=(args.label,) if args.label else LABELS)
    print(metrics_table(list(preds.values())).to_string())


if __name__ == "__main__":
    main()
