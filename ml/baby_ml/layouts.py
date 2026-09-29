"""The `StateLayout`s: the ways a training-set row can be written down for Jev.

Jev reads text, so every row has to become a `state`, a JSON object of named
facts. This file holds that conversion and nothing else: no API calls, no
caching (those are in `jev.py`).

- **`StateLayout`** is the abstract base class. A layout has a `name`, a
  `version`, and one method, `describe(row) -> dict`.
- **Four concrete layouts** subclass it, one per hypothesis (table below).
- **`LAYOUTS`** maps each name to an instance. `JevSpec(layout="minimal")`
  looks its layout up here, so a layout is chosen by name, never constructed.

To try a new layout, subclass `StateLayout`, add its name to `LayoutName`
and add an instance to `LAYOUTS`. Then read `example_states(df,
layout=...)` before running it. Bump `version` whenever an existing layout's
wording changes: it's part of Jev's cache key, and without the bump old
answers would be reused for new text.

*How* a row is written is as much a modelling choice as the trees'
hyperparameters, so each layout is one hypothesis about what Jev reads best:

| Layout        | Hypothesis                                                         |
|---------------|--------------------------------------------------------------------|
| `narrative`   | Short English sentences, arithmetic done in code (the original).   |
| `numeric`     | The same facts as raw numbers with unit-bearing keys: does doing   |
|               | the arithmetic for Jev actually help, or can it read numbers?      |
| `minimal`     | Only the four strongest facts: does trimming context sharpen it?   |
| `qualitative` | No numbers at all, only judgements ("near the end of her usual     |
|               | wake window"): plays to Jev's semantics, avoids its maths.         |

Every layout sees the same row, and none sees the label, the date or the
baby's name. Two of Jev's documented weak spots shaped `narrative`, and the
`val` results bore both out: it's unreliable at arithmetic and at comparing
times, so ratios and durations are computed here and handed over as words
(`numeric`, which skips that, was the worst layout); and irrelevant detail
lowers accuracy, so only the useful facts are sent (`minimal` did no worse
with half of them).
"""

from abc import ABC, abstractmethod
from typing import ClassVar, Literal

import pandas as pd
from typesafe_sdk import JSONValue

LayoutName = Literal["narrative", "numeric", "minimal", "qualitative"]


class StateLayout(ABC):
    """Turns one training-set row into the `state` Jev is shown."""

    name: ClassVar[LayoutName]
    version: ClassVar[int]

    @abstractmethod
    def describe(self, row: pd.Series) -> dict[str, JSONValue]:
        """The state for one row. Values must be plain Python (no numpy
        scalars), because they're serialised straight to JSON."""


class NarrativeLayout(StateLayout):
    """Named facts as short sentences, every ratio and duration pre-computed."""

    name = "narrative"
    version = 1

    def describe(self, row: pd.Series) -> dict[str, JSONValue]:
        return {
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


class MinimalLayout(StateLayout):
    """The narrative wording, cut to the four facts the trees rank highest.

    Age, clock, how far through her usual wake window she is, and where she is
    in her day. Drops last night, the 24-hour totals and feeding.
    """

    name = "minimal"
    version = 1

    def describe(self, row: pd.Series) -> dict[str, JSONValue]:
        full = NARRATIVE.describe(row)
        return {k: full[k] for k in ("baby", "time_now", "current_wake_window", "today_so_far")}


class NumericLayout(StateLayout):
    """The narrative's facts as raw numbers; the units live in the key names.

    Nothing is pre-computed beyond what the dbt features already are. The
    typical wake window is given, but not what share of it she's used, so any
    comparison is left to Jev.
    """

    name = "numeric"
    version = 1

    def describe(self, row: pd.Series) -> dict[str, JSONValue]:
        return {
            "age_weeks": int(row.age_weeks),
            "time_of_day_24h": _clock(row.minutes_since_midnight),
            "minutes_awake_since_last_sleep": int(row.minutes_since_last_wake),
            "last_sleep_type": "night sleep" if row.last_sleep_was_night else "nap",
            "last_sleep_duration_minutes": int(row.last_sleep_duration_minutes),
            "typical_wake_window_minutes_last_14_days": _optional_int(_typical_wake_window(row)),
            "last_3_wake_windows_average_minutes": int(round(row.avg_wake_window_last_3)),
            "minutes_since_waking_for_the_day": int(row.minutes_since_morning_wake),
            "naps_today": int(row.nap_count_today_so_far),
            "nap_minutes_today": int(row.nap_minutes_today_so_far),
            "last_night_sleep_minutes": int(row.last_night_sleep_minutes),
            "last_night_longest_stretch_minutes": int(row.last_night_longest_stretch_minutes),
            "last_night_wakings": int(row.last_night_waking_count),
            "sleep_minutes_last_24h": int(row.sleep_minutes_last_24h),
            "sleeps_last_24h": int(row.sleep_count_last_24h),
            "sleep_minutes_last_24h_vs_her_usual": int(round(row.sleep_debt_24h_minutes)),
            "minutes_since_last_feed_started": int(row.minutes_since_last_feed_start),
            "last_feed_duration_minutes": int(row.last_feed_duration_minutes),
            "last_feed_type": "bottle" if row.last_feed_type == "BOTTLE" else "breast",
            "feeds_last_24h": int(row.feed_count_last_24h),
            "average_minutes_between_feeds_last_24h": _optional_int(row.avg_feed_interval_last_24h),
            "cluster_feeding": bool(row.is_cluster_feeding),
        }


class QualitativeLayout(StateLayout):
    """Judgements, not measurements. The only number left is her age in months.

    Every threshold is relative to *her own* recent pattern where the data
    allows (wake window, daily sleep, feed interval), so "near the end of her
    usual wake window" means the same thing at 6 weeks and at 10 months.
    """

    name = "qualitative"
    version = 1

    def describe(self, row: pd.Series) -> dict[str, JSONValue]:
        return {
            "age": f"about {max(1, round(row.age_months))} months old",
            "time_of_day": _time_of_day(row.hour_of_day),
            "wake_window": _wake_window_judgement(
                row.wake_window_vs_recent_median, row.minutes_since_last_wake
            ),
            "last_sleep": _last_sleep_judgement(row),
            "day_so_far": _day_so_far_judgement(row),
            "last_night": _night_judgement(row.last_night_waking_count),
            "sleep_last_24h": _sleep_debt_judgement(row.sleep_debt_24h_minutes),
            "feeding": _feeding_judgement(row),
        }


NARRATIVE = NarrativeLayout()
LAYOUTS: dict[LayoutName, StateLayout] = {
    layout.name: layout
    for layout in (NARRATIVE, NumericLayout(), MinimalLayout(), QualitativeLayout())
}


# --- narrative helpers --------------------------------------------------------


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


# --- numeric helpers ----------------------------------------------------------


def _typical_wake_window(row: pd.Series) -> float | None:
    """The 14-day median wake window, recovered from the ratio feature.

    wake_window_vs_recent_median = minutes awake / median, so it can't be
    inverted at the very start of a wake window (0 / median = 0).
    """
    ratio = row.wake_window_vs_recent_median
    if row.minutes_since_last_wake > 0 and pd.notna(ratio) and ratio > 0:
        return row.minutes_since_last_wake / ratio
    return None


def _optional_int(value: float | None) -> int | None:
    return None if value is None or pd.isna(value) else int(round(value))


# --- qualitative helpers ------------------------------------------------------


def _time_of_day(hour: int) -> str:
    for end, label in (
        (5, "the middle of the night"),
        (8, "early morning"),
        (11, "mid-morning"),
        (13, "around midday"),
        (15, "early afternoon"),
        (17, "late afternoon"),
        (19, "early evening"),
        (21, "evening"),
        (23, "late evening"),
    ):
        if hour < end:
            return label
    return "the middle of the night"


def _wake_window_judgement(ratio: float, minutes_awake: int) -> str:
    if minutes_awake <= 10:
        return "she has only just woken up"
    if pd.isna(ratio) or ratio <= 0:
        return "no usual wake window to compare against"
    for limit, label in (
        (0.5, "early in her usual wake window"),
        (0.8, "past the halfway point of her usual wake window"),
        (1.0, "near the end of her usual wake window"),
        (1.3, "a little past her usual wake window"),
    ):
        if ratio < limit:
            return label
    return "well past her usual wake window"


def _last_sleep_judgement(row: pd.Series) -> str:
    if row.last_sleep_was_night:
        return "woke from her night sleep"
    minutes = row.last_sleep_duration_minutes
    if minutes < 45:
        return "woke from a short nap"
    if minutes < 90:
        return "woke from a medium-length nap"
    return "woke from a long nap"


_ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth")


def _day_so_far_judgement(row: pd.Series) -> str:
    naps = int(row.nap_count_today_so_far)
    if naps == 0:
        return "no naps yet today; this is her first stretch awake since the night"
    ordinal = _ORDINALS[naps - 1] if naps <= len(_ORDINALS) else "latest"
    return f"awake after her {ordinal} nap of the day"


def _night_judgement(wakings: int) -> str:
    if wakings == 0:
        return "slept through last night without waking"
    if wakings <= 2:
        return "woke once or twice last night"
    return "woke several times last night"


def _sleep_debt_judgement(debt: float) -> str:
    if debt < -90:
        return "well under her usual amount of sleep for a day"
    if debt < -30:
        return "a bit under her usual amount of sleep for a day"
    if debt <= 30:
        return "about her usual amount of sleep for a day"
    if debt <= 90:
        return "a bit more than her usual amount of sleep for a day"
    return "well over her usual amount of sleep for a day"


def _feeding_judgement(row: pd.Series) -> str:
    since = row.minutes_since_last_feed_start
    interval = row.avg_feed_interval_last_24h
    if since <= 30:
        text = "fed within the last half hour"
    elif pd.isna(interval) or interval <= 0:
        text = "fed a while ago"
    elif since / interval < 0.6:
        text = "fed a while ago, not due again yet"
    elif since / interval < 1.0:
        text = "getting close to when she usually feeds next"
    else:
        text = "due or overdue for a feed by her usual rhythm"
    if row.is_cluster_feeding:
        text += "; she is cluster feeding"
    return text
