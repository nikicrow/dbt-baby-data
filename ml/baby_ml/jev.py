"""Jev (TypeSafe's System One model) as a drop-in for the tree models.

Jev is not trained on our data. It reads text and answers typed questions, so
each prediction point becomes a short plain-English description of the baby's
state (the *state*), and each label becomes a yes/no *Noul* question whose
answer is a probability. That probability goes into the same `Predictions`
as the tree models, so every metric and chart applies unchanged.

What that makes this comparison: 20,000 labelled rows of *these two babies*
versus zero-shot general knowledge of infant sleep. Jev only sees the val rows
it's asked about, one at a time.

Two rules from Jev's documented weak spots shape `describe_state`:

- **Arithmetic stays in code.** Jev is unreliable at maths and at treating
  times as ordered quantities, so ratios, durations and comparisons ("awake for
  80% of her usual wake window") are computed here and handed over as words.
- **Send only what the question needs.** Low-signal columns (weekday, tod_sin,
  nappy counts) are left out; a feature the trees can ignore is noise to Jev.

API calls cost money and aren't free to repeat, so answers are cached per row
in `ml/data/jev/`, keyed by a hash of everything that could change them (model,
questions, state wording). Rerunning the notebook only calls the API for rows
it hasn't answered yet, and an interrupted run resumes where it stopped.
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
from baby_ml.settings import ML_DIR

CACHE_DIR = SNAPSHOT_DIR / "jev"

# Bump when describe_state's wording changes, so old cached answers (which
# were given to different text) aren't reused.
STATE_VERSION = 1

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

    @property
    def questions(self) -> dict[Label, Noul]:
        return {label: QUESTIONS[label] for label in self.labels}

    @property
    def cache_key(self) -> str:
        payload = {
            "model": self.model,
            "questions": {k: q.model_dump() for k, q in self.questions.items()},
            "state_version": STATE_VERSION,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return digest[:12]


# --- a row as words -------------------------------------------------------------


def describe_state(row: pd.Series) -> dict[str, str]:
    """The prediction point as short named facts, arithmetic already done."""
    state = {
        "baby": f"{row.age_weeks} weeks old ({row.age_months:.1f} months)",
        "time_now": f"{_clock(row.minutes_since_midnight)} ({_part_of_day(row.hour_of_day)})",
        "current_wake_window": _current_wake_window(row),
        "recent_wake_windows": (
            f"Her last three wake windows averaged {_duration(row.avg_wake_window_last_3)}."
        ),
        "today_so_far": _today_so_far(row),
        "last_night": (
            f"Slept {_duration(row.last_night_sleep_minutes)} in total, longest stretch "
            f"{_duration(row.last_night_longest_stretch_minutes)}, "
            f"woke {_times(row.last_night_waking_count)}."
        ),
        "sleep_last_24h": _sleep_last_24h(row),
        "feeding": _feeding(row),
    }
    return state


def _current_wake_window(row: pd.Series) -> str:
    awake = row.minutes_since_last_wake
    kind = "her night sleep" if row.last_sleep_was_night else "a nap"
    text = (
        f"Awake for {_duration(awake)}, since waking from {kind} "
        f"that lasted {_duration(row.last_sleep_duration_minutes)}."
    )
    # wake_window_vs_recent_median = awake / 14-day median wake window. Undo it
    # here so Jev gets both the typical length and the share, not a ratio.
    ratio = row.wake_window_vs_recent_median
    if awake > 0 and ratio and ratio > 0:
        typical = awake / ratio
        text += (
            f" Her typical wake window over the last two weeks is {_duration(typical)}, "
            f"so she is {ratio:.0%} of the way through a typical wake window."
        )
    return text


def _today_so_far(row: pd.Series) -> str:
    up = f"Up for the day for {_duration(row.minutes_since_morning_wake)}"
    if row.nap_count_today_so_far == 0:
        return f"{up}; no naps yet today."
    return (
        f"{up}; {_count(row.nap_count_today_so_far, 'nap')} so far today totalling "
        f"{_duration(row.nap_minutes_today_so_far)}."
    )


def _sleep_last_24h(row: pd.Series) -> str:
    debt = row.sleep_debt_24h_minutes
    if abs(debt) < 30:
        versus = "about her usual daily amount"
    elif debt < 0:
        versus = f"{_duration(-debt)} less than her usual daily amount"
    else:
        versus = f"{_duration(debt)} more than her usual daily amount"
    return (
        f"{_duration(row.sleep_minutes_last_24h)} across "
        f"{_count(row.sleep_count_last_24h, 'sleep')}, {versus}."
    )


def _feeding(row: pd.Series) -> str:
    kind = "bottle feed" if row.last_feed_type == "BOTTLE" else "breastfeed"
    text = (
        f"Last feed started {_duration(row.minutes_since_last_feed_start)} ago "
        f"(a {_duration(row.last_feed_duration_minutes)} {kind}). "
        f"{_count(row.feed_count_last_24h, 'feed')} in the last 24 hours"
    )
    if pd.notna(row.avg_feed_interval_last_24h):
        text += f", about every {_duration(row.avg_feed_interval_last_24h)}"
    text += "."
    if row.is_cluster_feeding:
        text += " She is cluster feeding."
    return text


def _duration(minutes: float) -> str:
    minutes = int(round(minutes))
    hours, mins = divmod(minutes, 60)
    if hours == 0:
        return f"{mins} min"
    return f"{hours} h" if mins == 0 else f"{hours} h {mins} min"


def _clock(minutes_since_midnight: int) -> str:
    hours, mins = divmod(int(minutes_since_midnight), 60)
    return f"{hours:02d}:{mins:02d}"


def _part_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _times(n: int) -> str:
    return {0: "no times", 1: "once", 2: "twice"}.get(int(n), f"{int(n)} times")


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
        return "jev"

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
        semaphore = asyncio.Semaphore(self.concurrency)
        async with self._client() as client:

            async def ask(row: pd.Series) -> None:
                async with semaphore:
                    response = await client.system_one(
                        state=describe_state(row),
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


def example_states(df: pd.DataFrame, n: int = 3, split: SplitName = "val") -> list[dict[str, str]]:
    """A few rendered states, to read exactly what Jev will be shown."""
    rows = df[df["split_forward_time"] == split].sample(n, random_state=1)
    return [describe_state(row) for _, row in rows.iterrows()]

