"""Offline adapter checks: leakage, cache identity, and positive-class ordering."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from baby_ml.tabicl import LABELS, TabICLModel, TabICLSpec


def fixture():
    return pd.DataFrame({
        'prediction_id': range(8), 'split_forward_time': ['train'] * 4 + ['val'] * 2 + ['test'] * 2,
        'feature': range(8), 'is_asleep_30_mins': [0, 1] * 4,
        'is_asleep_60_mins': [0, 1] * 4,
    })


class AdapterTests(unittest.TestCase):
    def test_rejects_future_and_overlapping_context(self):
        for context, split in [(('train', 'val'), 'val'), (('test',), 'val'), (('test',), 'test')]:
            with self.assertRaises(ValueError):
                TabICLModel(TabICLSpec(context_splits=context)).predict(fixture(), split)

    def test_cache_tracks_data_and_order(self):
        model, df = TabICLModel(), fixture()
        path = model.cache_path(df, LABELS[0], 'val')
        changed = df.copy()
        changed.loc[0, 'feature'] = 99
        self.assertNotEqual(path, model.cache_path(changed, LABELS[0], 'val'))
        self.assertNotEqual(path, model.cache_path(df.iloc[::-1], LABELS[0], 'val'))

    def test_positive_class_and_cached_roundtrip(self):
        class Classifier:
            classes_ = np.array([1, 0])
            def predict_proba(self, X):
                return np.tile([0.8, 0.2], (len(X), 1))
        model, df = TabICLModel(), fixture()
        with tempfile.TemporaryDirectory() as tmp, patch('baby_ml.tabicl.SNAPSHOT_DIR', Path(tmp)):
            with patch.object(model, 'fit', return_value=Classifier()) as fit:
                preds = model.predict(df, 'val')
                self.assertEqual(fit.call_count, 2)
            with patch.object(model, 'fit', side_effect=AssertionError('cache miss')):
                cached = model.predict(df, 'val', cache_only=True)
            for label in LABELS:
                np.testing.assert_allclose(preds[label].p, [0.8, 0.8])
                np.testing.assert_array_equal(preds[label].p, cached[label].p)


if __name__ == '__main__':
    unittest.main()
