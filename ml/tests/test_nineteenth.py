import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from baby_ml.nineteenth import LABELS, api, fit, prepare, read, probability


class TrainingTests(unittest.TestCase):
    def test_documented_and_live_probability_shapes(self):
        self.assertEqual(probability({"probability": 0.3}), 0.3)
        self.assertEqual(probability({"event_probability": 0.7}), 0.7)
        with self.assertRaises(ValueError):
            probability({"label": 1})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "source.parquet"
        self.output = self.root / "run"
        pd.DataFrame({
            "prediction_id": list("abcdefg"), "baby_id": ["private-id"] * 7,
            "baby_name": ["private-name"] * 7,
            "prediction_time": pd.to_datetime([
                "2026-01-01 10:00", "2026-01-01 11:00", "2026-01-02 09:30",
                "2026-01-02 10:00", "2026-01-02 11:00",
                "2026-01-03 10:00", "2026-01-03 11:00"]),
            "split_forward_time": ["train"] * 3 + ["val"] * 2 + ["test"] * 2,
            LABELS[0]: [0, 1, 0, 0, 1, 0, 1], LABELS[1]: [0, 1, 1, 0, 1, 0, 1],
            "minutes_to_next_sleep": [100, 10, 40, 100, 10, 100, 10],
            "age_days": [10] * 7, "minutes_since_last_wake": [20] * 7,
        }).to_parquet(self.snapshot)

    def test_prepare_excludes_future_and_identity_and_purges_boundary(self):
        manifest = prepare(self.snapshot, self.output)
        self.assertEqual(manifest["purged_rows"], 1)
        csv = pd.read_csv(self.output / LABELS[0] / "train.csv")
        self.assertEqual(list(csv), ["age_days", "minutes_since_last_wake", LABELS[0]])
        self.assertEqual(len(csv), 2)
        self.assertEqual(read(self.output / LABELS[0] / "request.json")["spec"]["positive_label"], 1)

    def test_submission_order_and_resume_uses_get(self):
        prepare(self.snapshot, self.output)
        folder = self.output / LABELS[0]
        pending = {"state": "training", "run_id": "abc", "status_url": "/v1/fit/abc?inline=false"}
        with patch("baby_ml.nineteenth.api", return_value=pending) as call:
            fit(folder, "test-key")
            body = call.call_args.args[2]
            self.assertLess(body.index(b'name="request"'), body.index(b'name="file"'))
            self.assertEqual(call.call_args.args[4], read(folder / "submission.json")["idempotency_key"])
            fit(folder, "test-key")
            self.assertEqual(len(call.call_args.args), 2)

    def test_replay_rejects_modified_csv(self):
        prepare(self.snapshot, self.output)
        folder = self.output / LABELS[0]
        with (folder / "train.csv").open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaisesRegex(ValueError, "CSV changed"):
            fit(folder, "test-key")

    def test_credentials_reject_unknown_origin(self):
        with self.assertRaises(ValueError):
            api("https://example.org/status", "test-key")


if __name__ == "__main__":
    unittest.main()
