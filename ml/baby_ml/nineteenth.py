"""19th probability models: prepare locally, submit/resume, then evaluate.

API contracts: https://19th.com/docs/training and /docs/inference.
Run ``python -m baby_ml.nineteenth --help`` for the command line interface.
"""

import argparse
import hashlib
import json
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import numpy as np
import pandas as pd
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from baby_ml.data import latest_snapshot, load_training_set
from baby_ml.evaluation import Predictions, metrics_table
from baby_ml.features import FeatureSpec
from baby_ml.settings import ML_DIR

ORIGIN = "https://training.19th.com"
LABELS = ("is_asleep_30_mins", "is_asleep_60_mins")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Refusing authenticated API redirect")


class Settings(BaseSettings):
    training_api_key: SecretStr
    model_config = SettingsConfigDict(env_file=ML_DIR / ".env", extra="ignore")


def save(path, value):
    """Atomic checkpoints keep interrupted runs resumable."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def probability(item):
    # The live runtime uses event_probability; the docs show probability.
    value = item.get("event_probability", item.get("probability"))
    if value is None or not np.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Missing or invalid event probability")
    return float(value)


def api(url, key, body=None, content_type="application/json", idempotency=None):
    """Only send credentials to documented API origins; never to downloads."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in {
        "training.19th.com", "inference.get19th.com"
    }:
        raise ValueError("Refusing API credentials for an unexpected origin")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": content_type}
    if idempotency:
        headers["Idempotency-Key"] = idempotency
    # No automatic POST retries: rerun with the persisted request and key.
    with build_opener(NoRedirect).open(Request(url, data=body, headers=headers), timeout=60) as response:
        return json.load(response)


def prepare(snapshot, output, mode="standard"):
    if output.exists():
        raise ValueError("Output already exists; resume it or choose a new directory")
    df = load_training_set(path=snapshot)
    if df.prediction_id.duplicated().any():
        raise ValueError("Duplicate prediction IDs")
    if not df[list(LABELS)].isin([0, 1]).all().all():
        raise ValueError("Both targets must be non-null binary labels")
    if (df[LABELS[0]] > df[LABELS[1]]).any():
        raise ValueError("30-minute positives must also be 60-minute positives")
    if not df.split_forward_time.isin(["train", "val", "test"]).all():
        raise ValueError("Unexpected split")
    # Purge labels whose 60-minute lookahead reaches the next split, per baby.
    # Splits are chronological within each baby, not across their birth years.
    keep = pd.Series(True, index=df.index)
    for _, baby in df.groupby("baby_id", observed=True):
        for before, after in (("train", "val"), ("val", "test")):
            a = baby[baby.split_forward_time == before]
            b = baby[baby.split_forward_time == after]
            if a.empty or b.empty or a.prediction_time.max() >= b.prediction_time.min():
                raise ValueError("Each baby needs ordered, nonempty train/val/test splits")
            keep.loc[a.index] = a.prediction_time + pd.Timedelta(minutes=60) < b.prediction_time.min()
    features = FeatureSpec().feature_columns(df)
    # Explicit guard for a known future-derived field if a new snapshot adds it.
    features = [c for c in features if c != "enclosing_gap_minutes"]
    if not features:
        raise ValueError("No input features")
    numeric = df[features].select_dtypes(include="number")
    if np.isinf(numeric.to_numpy(dtype=float)).any():
        raise ValueError("Infinite feature value")
    rows = df[keep].copy()
    train = rows[rows.split_forward_time == "train"]
    if any(train[label].nunique() != 2 for label in LABELS):
        raise ValueError("Each training target needs both classes")
    output.mkdir(parents=True)
    # Frozen local holdouts retain IDs for alignment; IDs never leave the machine.
    rows.to_parquet(output / "snapshot.parquet", index=False)
    summary = rows.groupby("split_forward_time", observed=True).agg(
        rows=("prediction_id", "size"), rate30=(LABELS[0], "mean"), rate60=(LABELS[1], "mean")
    ).to_dict(orient="index")
    manifest = {"source": str(snapshot.resolve()), "source_sha256": digest(snapshot.read_bytes()),
                "input_columns": features, "purged_rows": int((~keep).sum()), "splits": summary}
    for label in LABELS:
        folder = output / label
        folder.mkdir()
        csv = train[features + [label]].to_csv(index=False, lineterminator="\n").encode("utf-8")
        (folder / "train.csv").write_bytes(csv)
        request = {"name": f"Baby sleep within {30 if label == LABELS[0] else 60} minutes",
                   "file": {"content_length": len(csv), "sha256": digest(csv)},
                   "spec": {"target_column": label, "input_columns": features,
                            "output_kind": "event_probability", "positive_label": 1,
                            "training_mode": mode},
                   "model_variant": "fast", "wait": False, "inline": False}
        save(folder / "request.json", request)
        save(folder / "submission.json", {"idempotency_key": str(uuid.uuid4()),
                                           "request_sha256": digest(json.dumps(request, sort_keys=True).encode())})
    manifest["frozen_sha256"] = digest((output / "snapshot.parquet").read_bytes())
    save(output / "manifest.json", manifest)
    return manifest


def fit(folder, key):
    """One status read per invocation, or a replay-safe first submission."""
    result_path = folder / "fit.json"
    if result_path.exists():
        prior = read(result_path)
        url = urljoin(ORIGIN, prior["status_url"])
        if urlsplit(url).netloc != urlsplit(ORIGIN).netloc:
            raise ValueError("Status URL must stay on training origin")
        result = api(url, key)
    else:
        request = read(folder / "request.json")
        submission = read(folder / "submission.json")
        csv = (folder / "train.csv").read_bytes()
        if request["file"] != {"content_length": len(csv), "sha256": digest(csv)}:
            raise ValueError("Training CSV changed after preparation")
        if digest(json.dumps(request, sort_keys=True).encode()) != submission["request_sha256"]:
            raise ValueError("Request changed after preparation; create a new bundle")
        boundary = "babyml-" + submission["idempotency_key"]
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="request"\r\n'
                'Content-Type: application/json\r\n\r\n').encode() + json.dumps(request).encode()
        body += (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="train.csv"\r\n'
                 'Content-Type: text/csv\r\n\r\n').encode() + csv
        body += f"\r\n--{boundary}--\r\n".encode()
        result = api(ORIGIN + "/v1/fit", key, body,
                     f"multipart/form-data; boundary={boundary}", submission["idempotency_key"])
    save(result_path, result)
    if result["state"] in {"failed", "cancelled"}:
        raise RuntimeError(f"Fit {result['state']}; inspect {result_path}")
    return {k: result.get(k) for k in ("run_id", "state", "download_state", "predictions_state")}


def evaluate(output, key, split="val"):
    manifest = read(output / "manifest.json")
    frozen = output / "snapshot.parquet"
    if digest(frozen.read_bytes()) != manifest["frozen_sha256"]:
        raise ValueError("Frozen evaluation snapshot changed")
    df = pd.read_parquet(frozen)
    rows = df[df.split_forward_time == split]
    records = json.loads(rows[manifest["input_columns"]].to_json(orient="records"))
    predictions = []
    for label in LABELS:
        folder = output / label
        result = read(folder / "fit.json")
        hosted = result.get("model", {}).get("prediction", {})
        if result["state"] != "ready" or hosted.get("state") != "ready":
            raise RuntimeError("Model not ready for inference; run fit again to refresh status")
        cache_dir = folder / split
        cache_dir.mkdir(exist_ok=True)
        probabilities = []
        for start in range(0, len(records), 100):
            batch = records[start:start + 100]
            payload = json.dumps({"input": batch}, allow_nan=False).encode()
            cache = cache_dir / f"{start:06d}-{digest(payload)[:16]}.json"
            if cache.exists():
                response = read(cache)
            else:
                response = api(hosted["url"], key, payload)
                save(cache, response)
            values = [probability(item) for item in response["output"]]
            if len(values) != len(batch) or not all(np.isfinite(p) and 0 <= p <= 1 for p in values):
                raise ValueError("Invalid probability response")
            probabilities.extend(values)
            if start % 1000 == 0 or start + len(batch) == len(records):
                print(f"{label}: {start + len(batch)}/{len(records)} {split} rows", flush=True)
        p = np.array(probabilities)
        predictions.append(Predictions(name="19th", label=label, split=split,
                                       y_true=rows[label].to_numpy(), p=p))
        pd.DataFrame({"prediction_id": rows.prediction_id, "y_true": rows[label], "p": p}).to_parquet(
            folder / f"{split}-predictions.parquet", index=False)
    metrics = metrics_table(predictions)
    metrics.to_csv(output / f"{split}-metrics.csv")
    # Independent models can disagree across horizons. Report this, don't silently alter scores.
    violations = float((predictions[0].p > predictions[1].p).mean())
    save(output / f"{split}-consistency.json", {"fraction_p30_above_p60": violations})
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "fit", "evaluate"])
    parser.add_argument("--output", type=Path, default=ML_DIR / "data" / "19th" / "sleep-v1")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--mode", choices=["instant", "standard", "high", "ultra"], default="standard")
    parser.add_argument("--split", choices=["val", "test"], default="val")
    args = parser.parse_args()
    if args.command == "prepare":
        snapshot = args.snapshot or latest_snapshot()
        if snapshot is None:
            parser.error("No local snapshot; pull one with load_training_set(refresh=True)")
        print(json.dumps(prepare(snapshot, args.output, args.mode), indent=2))
    else:
        key = Settings().training_api_key.get_secret_value()
        if args.command == "fit":
            for label in LABELS:
                print(label, fit(args.output / label, key), flush=True)
        else:
            print(evaluate(args.output, key, args.split).to_string())


if __name__ == "__main__":
    main()
