"""Ask Jev about a split of the training set and get back `Predictions`.

This file turns Jev into something the rest of `baby_ml` can treat like any
other model. The trees are fitted on rows; Jev is sent each row as text (via a
layout from `layouts.py`) with one yes/no question per label, and its
probabilities come back as the same `Predictions` the trees produce. So
`metrics_table`, the curves and `Comparison.with_predictions` all work on it
unchanged.

The pieces, in the order they're used:

- **`JevSettings`** reads the API key from `TYPESAFE_API_KEY` in `ml/.env`
  (gitignored) as a `SecretStr`. You never construct it yourself;
  `JevModel` does when it opens a client.
- **`QUESTIONS`** holds the fixed wording of the two yes/no questions, one per
  label.
- **`JevSpec`** is *what* to ask: model version, labels, split column and
  layout. It's a frozen Pydantic model because it's also the cache's identity:
  `cache_key` hashes everything that could change Jev's answer, so changing
  any of it starts a fresh cache instead of mixing answers to different
  questions. It plays the same role as `FeatureSpec` does for the trees.
- **`JevModel`** is *how* to ask: it holds a spec plus run settings
  (concurrency, how often to save) that don't affect the answers, and so
  aren't in the key. `predict` sends only the rows that aren't cached yet,
  saves as it goes, and returns one `Predictions` per label.
- **`example_states` / `describe_state`** show exactly what Jev will read,
  without calling the API. Check these before spending money on a new layout.

Usage, in a notebook (Jupyter allows top-level `await`):

    jev = JevModel(spec=JevSpec(layout="minimal"))
    preds = await jev.predict(df, "val")          # {label: Predictions}
    comparison = Comparison.run(df).with_predictions(list(preds.values()))
    jev.cost()                                    # tokens and USD spent so far

Answers are cached per row in `ml/data/jev/<cache_key>.parquet`, so rerunning
costs nothing, and a run that dies partway resumes from the last save.
"""

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from typesafe_sdk import AsyncTypeSafeClient, Noul, RetryPolicy

from baby_ml.data import SNAPSHOT_DIR
from baby_ml.evaluation import Predictions
from baby_ml.features import Label, SplitColumn, SplitName
from baby_ml.layouts import LAYOUTS, LayoutName, StateLayout
from baby_ml.settings import ML_DIR

CACHE_DIR = SNAPSHOT_DIR / "jev"

# USD per million input tokens; output tokens are free. From docs.typesafe.ai/models.
PRICE_PER_MILLION_INPUT_TOKENS = 0.042


class JevSettings(BaseSettings):
    """The API key, read from `TYPESAFE_API_KEY` in the environment or `ml/.env`.

    `ml/.env` is gitignored. The key is a SecretStr, so it never shows in a
    repr, a traceback or notebook output.
    """

    api_key: SecretStr
    base_url: str | None = None

    model_config = SettingsConfigDict(
        env_prefix="TYPESAFE_", env_file=ML_DIR / ".env", extra="ignore"
    )


# --- the questions ------------------------------------------------------------

QUESTIONS: dict[Label, Noul] = {
    "is_asleep_30_mins": Noul(
        instructions="Will this baby fall asleep within the next 30 minutes?",
        criteria={
            "true": "She starts a nap or her night sleep at some point in the next 30 minutes.",
            "false": "She is still awake 30 minutes from now.",
        },
    ),
    "is_asleep_60_mins": Noul(
        instructions="Will this baby fall asleep within the next 60 minutes?",
        criteria={
            "true": "She starts a nap or her night sleep at some point in the next 60 minutes.",
            "false": "She is still awake 60 minutes from now.",
        },
    ),
}


class JevSpec(BaseModel):
    """Everything that decides Jev's answer, besides the row itself.

    Its hash names the cache file, so changing the model, a question or the
    state wording starts a fresh cache rather than mixing answers.
    """

    model_config = ConfigDict(frozen=True)

    model: str = "jev-latest"
    labels: tuple[Label, ...] = ("is_asleep_30_mins", "is_asleep_60_mins")
    split_column: SplitColumn = "split_forward_time"
    layout: LayoutName = "narrative"

    @property
    def state_layout(self) -> StateLayout:
        return LAYOUTS[self.layout]

    @property
    def questions(self) -> dict[Label, Noul]:
        return {label: QUESTIONS[label] for label in self.labels}

    @property
    def cache_key(self) -> str:
        payload = {
            "model": self.model,
            "questions": {k: q.model_dump() for k, q in self.questions.items()},
            "layout": self.layout,
            "layout_version": self.state_layout.version,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return digest[:12]


# --- calling the API -------------------------------------------------------------


class JevModel(BaseModel):
    """Asks Jev about each row of a split and returns `Predictions` per label.

    >>> jev = JevModel()
    >>> preds = await jev.predict(df, "val")       # top-level await in Jupyter
    >>> metrics_table(list(preds.values()))
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    spec: JevSpec = JevSpec()
    concurrency: int = 16  # the API allows 1,200 requests a minute
    save_every: int = 200  # rows between cache writes, so a crash loses little
    # Swappable so tests can run without the API.
    client_factory: Callable[[], Any] | None = None

    @property
    def name(self) -> str:
        return f"jev_{self.spec.layout}"

    @property
    def cache_path(self) -> Path:
        return CACHE_DIR / f"{self.spec.cache_key}.parquet"

    def cached(self) -> pd.DataFrame:
        if self.cache_path.exists():
            return pd.read_parquet(self.cache_path)
        return pd.DataFrame(columns=["prediction_id", *self.spec.labels, "input_tokens", "jev_model"])

    async def predict(
        self, df: pd.DataFrame, split: SplitName = "val", limit: int | None = None
    ) -> dict[Label, Predictions]:
        """Jev's probabilities for every row of `split`, one `Predictions` per label.

        Rows already in the cache aren't sent again. `limit` scores a fixed
        random sample instead, for a cheap smoke test — its metrics are on
        different rows from the trees', so don't compare them.
        """
        rows = df[df[self.spec.split_column] == split]
        if limit is not None and limit < len(rows):
            rows = rows.sample(limit, random_state=0)

        answers = await self._answer(rows)
        answers = answers.set_index("prediction_id").loc[rows["prediction_id"]]
        return {
            label: Predictions(
                name=self.name,
                label=label,
                split=split,
                y_true=rows[label].to_numpy(),
                p=answers[label].to_numpy(dtype=float),
            )
            for label in self.spec.labels
        }

    def cost(self) -> dict[str, float]:
        """Tokens and dollars spent so far on this spec's cached answers."""
        tokens = int(self.cached()["input_tokens"].fillna(0).sum())
        return {
            "rows": len(self.cached()),
            "input_tokens": tokens,
            "usd": tokens / 1e6 * PRICE_PER_MILLION_INPUT_TOKENS,
        }

    async def _answer(self, rows: pd.DataFrame) -> pd.DataFrame:
        cache = self.cached()
        todo = rows[~rows["prediction_id"].isin(cache["prediction_id"])]
        if todo.empty:
            return cache

        print(f"Asking Jev about {len(todo):,} rows ({len(rows) - len(todo):,} cached)")
        new: list[dict[str, Any]] = []
        # A semaphore is a counter of free slots: `async with semaphore` takes
        # a slot, or waits until one is free, and gives it back on exit. All
        # the chunk's requests are started at once by gather() below, but only
        # `concurrency` (16) can be inside the `async with` block, waiting on
        # the API, at any moment; the rest queue. Without it, gather would
        # fire 200 requests at the same instant and hit the rate limit.
        semaphore = asyncio.Semaphore(self.concurrency)
        async with self._client() as client:

            async def ask(row: pd.Series) -> None:
                async with semaphore:
                    response = await client.system_one(
                        state=self.spec.state_layout.describe(row),
                        questions=self.spec.questions,
                        model=self.spec.model,
                    )
                new.append(
                    {
                        "prediction_id": row.prediction_id,
                        **{label: response.answers[label].noul for label in self.spec.labels},
                        "input_tokens": response.usage.input_tokens,
                        "jev_model": response.model,
                    }
                )

            records = [row for _, row in todo.iterrows()]
            for start in range(0, len(records), self.save_every):
                chunk = records[start : start + self.save_every]
                # Let the whole chunk finish before looking at failures, so
                # every answer that did come back is saved. A request that
                # still fails after retries then stops the run; rerunning
                # picks up from the cache.
                results = await asyncio.gather(*(ask(row) for row in chunk), return_exceptions=True)
                cache = self._save(cache, new)
                new = []
                errors = [r for r in results if isinstance(r, BaseException)]
                if errors:
                    raise errors[0]
                print(f"  {min(start + self.save_every, len(records)):,} / {len(records):,}")
        return cache

    def _save(self, cache: pd.DataFrame, new: Sequence[dict[str, Any]]) -> pd.DataFrame:
        if not new:
            return cache
        combined = pd.concat([cache, pd.DataFrame(new)], ignore_index=True)
        combined = combined.drop_duplicates("prediction_id", keep="last")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        combined.to_parquet(self.cache_path, index=False)
        return combined

    def _client(self):
        if self.client_factory is not None:
            return self.client_factory()
        settings = JevSettings()
        return AsyncTypeSafeClient(
            api_key=settings.api_key.get_secret_value(),
            base_url=settings.base_url,
            retry=RetryPolicy(max_retries=5, backoff_max=20.0, timeout=60.0),
        )


def example_states(
    df: pd.DataFrame, n: int = 3, split: SplitName = "val", layout: LayoutName = "narrative"
) -> list[dict[str, Any]]:
    """A few rendered states, to read exactly what Jev will be shown.

    The same rows for every layout, so layouts can be compared side by side.
    """
    rows = df[df["split_forward_time"] == split].sample(n, random_state=1)
    return [LAYOUTS[layout].describe(row) for _, row in rows.iterrows()]


def describe_state(row: pd.Series, layout: LayoutName = "narrative") -> dict[str, Any]:
    """One row as Jev would see it under `layout`."""
    return LAYOUTS[layout].describe(row)

