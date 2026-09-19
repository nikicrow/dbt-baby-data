# ML Sleep Prediction — Feature & Label Layer

**Status: DRAFT — awaiting your approval. No code written yet.**

Goal: predict, at any moment when the baby is awake, whether she will fall
asleep in the next 30 minutes (and separately, the next 60). This document
plans only the two data assets that make that possible — a **label table** and
a **feature table**. Model training (logistic regression → random forest →
XGBoost → LightGBM, then comparison against newer approaches) is a later phase
and is sketched at the end only so the data design doesn't paint us into a
corner.

---

## 0. What the data actually looks like

I measured this from the seeds in this repo rather than guessing, because it
changes several design decisions.

| | Ember | Imogen |
|---|---|---|
| Date of birth | 2023-08-18 | 2026-03-13 |
| Sleep records | 1,734 | 1,100 |
| Tracked range | 2023-09-13 → 2024-08-17 | 2026-03-15 → 2026-08-25 |
| Age at first log | 26 days | 2 days |
| Age at last log | 365 days | 165 days |
| Calendar days in range | 340 | 164 |
| Days with **zero** sleep logs | **0** | **0** |
| Sleeps logged per day (median) | 5 | 7 |
| Gap between consecutive sleeps, p50 / p90 / p99 | 99 / 181 / 271 min | 79 / 150 / 229 min |
| Awake gaps > 6 h (probable tracking holes) | 6 | 1 |
| Awake gaps > 8 h | 2 | 0 |
| Overlapping sleep records | 8 (0.5 %) | 2 (0.2 %) |
| Nursing records | 2,038 | 1,260 |
| Diaper records | 2,322 | 1,224 |
| Time asleep | 62.8 % | 59.2 % |
| Total awake time | 3,033 h | 1,603 h |

**The tracking is good enough for this to work.** Every single calendar day in
both babies' ranges has at least one sleep record, and the 99th-percentile gap
between sleeps is about 4 hours. That matters enormously, because our negative
label ("she did *not* fall asleep") is only trustworthy if an absence of a sleep
record genuinely means she was awake, rather than meaning nobody logged it.
With this density, it mostly does. The handful of >8 h gaps get flagged and
dropped (§3.4).

### What we get if we sample the awake time on a fixed grid

I simulated the label table to check size and class balance before committing
to a design:

| Grid | Ember rows | Imogen rows | Total | `is_asleep_30_mins` = 1 | `is_asleep_60_mins` = 1 |
|---|---|---|---|---|---|
| 5 min | 36,408 | 19,205 | 55,613 | 29.5 % | 54.4 % |
| **10 min** | **18,199** | **9,615** | **27,814** | **29.5 %** | **54.4 %** |
| 15 min | 12,130 | 6,395 | 18,525 | 29.5 % | 54.4 % |
| 30 min | 6,070 | 3,195 | 9,265 | 29.5 % | 54.4 % |

Per baby the base rates are Ember 27.3 % / 50.5 % and Imogen 33.7 % / 61.6 %.

Two things fall out of this table:

1. **The class balance is fine.** Roughly 30/70 at 30 minutes and almost 50/50
   at 60 minutes. No resampling, no SMOTE, no synthetic minority anything.
2. **The tables are small.** 28k rows × ~55 columns is a few megabytes. This
   answers your sizing worry directly — see §5.

And the signal is visibly there before we model anything. Probability she falls
asleep within 30 minutes, by hour of day (Ember): **15 % at 8 am, 16 % at 1 pm,
68 % at 5 am**. A model that only knew the clock would already beat the base
rate.

---

## 1. Design decisions I want you to sign off on

These are the five choices that shape everything else. I've made a
recommendation on each; overrule any of them.

### 1.1 "Asleep in the next 30 minutes" means *sleep onset*, not *state at t+30*

There are two readings of the label and they are genuinely different:

- **Onset-based** (recommended): 1 if a sleep session *starts* at any point in
  the window `(t, t+30min]`.
- **State-based**: 1 if she is asleep *at exactly* `t + 30min`.

They disagree whenever she falls asleep at t+5 and wakes at t+25 — onset says
1, state says 0. I recommend onset because it matches the question you'd
actually ask the model: *"is it worth starting to settle her right now?"* A
20-minute catnap that starts in 5 minutes is still a yes.

Both are cheap to compute, so the label table will carry **both**, with the
onset version as the primary `is_asleep_30_mins` / `is_asleep_60_mins` and the
state version as `is_asleep_at_30_mins` / `is_asleep_at_60_mins` for
comparison. Costs us two integer columns.

### 1.2 Use a regular 10-minute grid, not random times

You asked for random times. I'd push back, and here's why:

- **28k rows is not big.** There's no pressure to subsample, so a random sample
  buys us nothing and costs reproducibility — rebuild the table and you get a
  different training set unless we pin a seed through the SQL, which is fiddly
  in Postgres.
- **A grid is fully reconstructable.** Anyone can regenerate the exact same
  table from the raw data, which matters when you're comparing model families
  and want to be sure the difference is the model and not the sample.
- **Day/night coverage comes out honest.** A grid preserves the natural
  proportion of night-time rows. Random sampling at a uniform rate over
  wall-clock time does exactly the same thing, so randomness isn't buying you
  the night coverage you wanted — the grid already has it. (Ember has 638 awake
  night-time rows between midnight and 6 am; Imogen 932.)

You keep the option anyway: the table will carry `sample_bucket`, a
deterministic 0–99 hash of `(baby_id, prediction_time)`. Any reproducible
random subsample is then one `where sample_bucket < 20` away.

**But be aware of what the 28k really is.** Consecutive rows 10 minutes apart
are almost the same observation — same wake window, same last feed, clock 10
minutes later. The *effective* sample size is far closer to the number of wake
windows (~2,800 for Ember, ~1,900 for Imogen) than to 28,000. Practical
consequences: grouped splitting is mandatory (§1.3), and any confidence
interval computed as if we had 28k independent rows will be far too narrow.
I'll re-run the headline metrics on a 30-minute grid as a sanity check.

### 1.3 Split by **day**, never by row

This is the single most important decision in the document. If we split rows
randomly, the row at 14:30 lands in train and the row at 14:40 lands in test,
and they are near-identical — the model looks brilliant and generalises to
nothing.

I propose three splits, all defined as columns on the label table so every
model family uses byte-identical data:

| Column | How | What it answers |
|---|---|---|
| `split_random_day` | `train` / `val` / `test` = 70/15/15, assigned per `(baby_id, calendar_date)` by `md5()` hash | Standard benchmark. Grouped by day so no window straddles the boundary. |
| `split_forward_time` | Last 20 % of each baby's calendar range = `test` | Honest answer to "does a model fit on old data still work next month?" — which is the real production setting. Also catches age-drift overfitting. |
| `split_holdout_baby` | Ember = `train`, Imogen = `test` (and the reverse, as `split_holdout_baby_rev`) | The question that actually matters: **does this work for a baby it has never seen?** Expect the metrics here to be meaningfully worse than the other two. |

Report all three. If `split_random_day` looks great and `split_holdout_baby`
looks like the base rate, we've built a Ember-and-Imogen memoriser, and better
to know that early.

A test set is only a test set if it stays sealed. The plan is to run the whole
model bake-off against `val`, and touch `test` exactly once at the end.

### 1.4 Never put `baby_name` (or calendar date) into the model

`baby_name` is in the table for grouping and splitting. Feeding it to the model
guarantees it can't generalise to a third baby. Same for raw calendar date: with
one baby per era, "October 2023" *is* "Ember at 8 weeks", so a date feature lets
the model identify the baby through the back door and silently defeats the
leave-one-baby-out split. `age_days` is legitimate and stays; `metric_date` does
not.

### 1.5 The tables live in a new `ml` schema, built by dbt

New model directory `baby_data/models/ml/`, materialised as tables in a schema
literally named `ml`, exactly mirroring how `marts` works today (the existing
`generate_schema_name` macro already handles this — `+schema: ml` gets us
`ml.*` locally and `<target>_ml` in CI, so PR builds won't overwrite the real
tables). Nothing about the existing raw → staging → marts flow changes.

---

## 2. Table structure — how the join stays 1:1 and the tables stay small

This is the part you specifically asked me to think about, so I'll be explicit.

**The mistake to avoid** is materialising features for every minute of every
day and then filtering. That's 504 baby-days × 1,440 minutes = 726,000 rows,
most of them thrown away because the baby was asleep.

**The fix** is that both tables are generated from one shared spine, so they
have exactly the same grain, exactly the same number of rows, and join 1:1 on
a two-column key. We never build a row we won't use.

```
                  fct_sleep_sessions
                          │
                          ▼
              ml_prediction_points          ← THE SPINE. One row per
              (baby_id, prediction_time,       (baby, awake moment on the
               split_*, sample_bucket)         10-min grid). ~27,800 rows.
                     │        │                Defines the grain. Nothing
        ┌────────────┘        └────────────┐   else is allowed to change it.
        ▼                                  ▼
  ml_sleep_labels                   ml_sleep_features
  spine + 4 label cols              spine + ~55 feature cols
  ~27,800 rows                      ~27,800 rows
        │                                  │
        └──────────────┬───────────────────┘
                       ▼
            ml_sleep_training_set  (view)
            inner join on (baby_id, prediction_time)
            ~27,800 rows — what Python reads
```

Four models. `ml_prediction_points` is the contract: both children select
`from ml_prediction_points` and add columns, so the join cannot fan out or drop
rows by construction.

**Guardrails, as dbt tests:**

- `unique` on the surrogate key `prediction_id` (`baby_id || prediction_time`)
  in all three tables — proves the grain in each.
- `not_null` on the key columns.
- A custom test `assert_ml_tables_same_grain.sql`: `ml_sleep_labels`,
  `ml_sleep_features` and `ml_prediction_points` all have identical row counts
  and no unmatched keys in either direction. This fails loudly the moment
  someone's `where` clause quietly drops rows from one side.
- A custom test asserting no prediction point falls inside a sleep session.
- `accepted_values` on `split_*` columns.

**Rough disk cost:** ~27,800 rows × ~60 numeric columns ≈ 13 MB per table,
under 50 MB for the layer. Rebuild time on Postgres should be seconds. If we
ever add a third baby at the same tracking density, add ~10k rows. This scales
to a dozen babies before anyone needs to think about it again.

**Why not one wide table?** Because the label definitions will change (you may
want 45 minutes, or the state-based variant) far more often than the features
will, and vice versa. Separating them means a label change doesn't force a
recompute of 55 feature columns. The joined view costs nothing.

---

## 3. The label table — `ml_sleep_labels`

### 3.1 Grain

One row per `(baby_id, prediction_time)` where the baby was **awake** at
`prediction_time`, on a 10-minute grid.

### 3.2 Columns

| Column | Type | Meaning |
|---|---|---|
| `prediction_id` | text | `baby_id \|\| '_' \|\| prediction_time` — surrogate key |
| `baby_id` | uuid | FK to `raw_baby_profiles` |
| `baby_name` | text | For grouping/filtering only — **never a feature** |
| `prediction_time` | timestamp | The moment we're standing at, local time |
| `is_asleep_30_mins` | int 0/1 | **Primary label.** A sleep session starts in `(t, t+30min]` |
| `is_asleep_60_mins` | int 0/1 | **Primary label.** A sleep session starts in `(t, t+60min]` |
| `is_asleep_at_30_mins` | int 0/1 | Alternative: she is inside a sleep session at exactly `t+30min` |
| `is_asleep_at_60_mins` | int 0/1 | Alternative: same at `t+60min` |
| `minutes_to_next_sleep` | int | Raw time-to-event. Useful for diagnostics and for a survival-model comparison later |
| `split_random_day` | text | `train` / `val` / `test` |
| `split_forward_time` | text | `train` / `test` |
| `split_holdout_baby` | text | `train` / `test` |
| `split_holdout_baby_rev` | text | `train` / `test` |
| `sample_bucket` | int 0–99 | Deterministic hash, for reproducible subsampling |

### 3.3 How it's built

1. **Merge sleep intervals.** `fct_sleep_sessions` has 0.5 % overlapping
   records (one sleep logged across two entries). Merge any sessions where
   `start_time <= previous end_time` into one continuous asleep block, per baby.
   Without this, an overlap creates a spurious "awake" instant.
2. **Generate the grid.** `generate_series(first_log_date, last_log_date,
   interval '10 minutes')` per baby, via `cross join lateral` off the profile
   table — the same pattern `mart_daily_metrics` already uses for its date spine.
3. **Keep only awake moments.** Anti-join against the merged sleep blocks:
   keep `t` where no block satisfies `start <= t < end`.
4. **Apply the exclusions in §3.4.**
5. **Compute labels** by looking forward to the next sleep onset — a
   `lateral join ... order by start_time limit 1` against the merged blocks.

### 3.4 Rows we deliberately exclude

Each exclusion gets a `exclusion_reason` column in an intermediate model so we
can count what we dropped and why, rather than silently losing rows.

| Rule | Why | Approx rows lost |
|---|---|---|
| Baby is asleep at `t` | Your constraint — a prediction is only meaningful when she's awake | n/a, that's the filter itself |
| `t` is within 60 min of the baby's **last** log | We can't see forward far enough to know the label — labelling these 0 would be inventing negatives | 4 rows |
| `t` sits inside a wake gap longer than **6 hours** | Almost certainly a tracking hole, not a genuinely awake baby. A 9-hour "awake window" for a 3-month-old didn't happen | **405 rows** (7 gaps: 6 Ember, 1 Imogen) |
| `t` is in the first 24 h of a baby's tracking | No history, so every trailing feature is null | 138 rows |

The 6-hour threshold matches the existing `fct_wake_windows` model, which
already bounds windows to 5–360 minutes for exactly this reason. Worth keeping
the two consistent.

Net: **547 rows, about 2 %** — so the final table lands near **27,270**, not the
27,814 in the §0 grid table (that simulation counted raw awake grid points,
before exclusions). The dominant cost by far is the 6-hour gap rule, and it is
the one worth revisiting if you think any of those gaps were real awake time
rather than a day nobody logged.

---

## 4. The feature table — `ml_sleep_features`

### The one rule

**Every feature uses only events with a timestamp strictly at or before
`prediction_time`.** No exceptions. Because every row is an awake row, the
sleep we're predicting hasn't started yet, so there's no state to leak from —
but trailing aggregates are where this gets subtle, and §4.9 covers the traps.

### 4.1 Time of day and calendar — from `prediction_time` alone

| Column | Type | Notes |
|---|---|---|
| `hour_of_day` | int 0–23 | |
| `minutes_since_midnight` | int 0–1439 | Finer-grained than the hour |
| `tod_sin` | float | `sin(2π × minutes_since_midnight / 1440)` |
| `tod_cos` | float | `cos(...)`. **Needed for logistic regression** — without it, 23:50 and 00:10 are 1,420 units apart instead of 20. Trees don't need it but it costs nothing to carry |
| `is_night_hours` | bool | 19:00–07:00, matching `fct_sleep_sessions.is_night` |
| `day_of_week` | int 0–6 | Picks up parental routine — weekend lie-ins, weekday outings |
| `is_weekend` | bool | |

All timestamps are naive local time (Australia/Sydney) as loaded — no timezone
conversion needed, but worth asserting rather than assuming, since the profile
table carries a `timezone` column that nothing currently reads.

*Considered and rejected:* `month`, `season`. With under one year per baby,
"July" is nearly a unique identifier for a life stage. Straight to overfitting.

### 4.2 Age and development

| Column | Type | Notes |
|---|---|---|
| `age_days` | int | Already computed in staging |
| `age_weeks` | int | Already computed in staging |
| `age_months` | float | `age_days / 30.44`. Smoother for linear models |

Age is the strongest confounder in the whole dataset — wake windows roughly
double between 1 and 12 months — so it must be in the model. Just remember it's
collinear with calendar date within a baby (§1.4).

### 4.3 Current wake state — **expect these to be the top features**

| Column | Type | Notes |
|---|---|---|
| `minutes_since_last_wake` | int | Minutes since the end of the most recent sleep. *This is "how long has she been awake", and I'd bet on it being #1 by importance* |
| `last_sleep_duration_minutes` | int | How long the sleep she just woke from lasted — a 20-minute catnap leaves her ready to go again sooner than a 2-hour one |
| `last_sleep_was_night` | bool | From `fct_sleep_sessions.is_night` |
| `minutes_since_last_sleep_start` | int | Onset-to-onset spacing rather than wake-to-now |
| `minutes_since_morning_wake` | int | Minutes since the night ended. Positions her in the day's rhythm better than the clock does, because bedtimes and wake times drift |
| `nap_number_today` | int | 0 if no naps yet since the night, else how many completed. The 3rd nap behaves differently from the 1st |
| `is_first_wake_of_day` | bool | The post-night wake window is structurally different |
| `wake_window_vs_recent_median` | float | `minutes_since_last_wake ÷ (median completed wake window over the **trailing 14 days**)`. Self-normalises away the age effect: 0.5 means "half her usual" whether she's 4 weeks or 40. Trailing, never global — see §4.9 |
| `avg_wake_window_last_3` | float | Mean of her 3 most recent completed wake windows — captures "she's having an off day" |

### 4.4 Recent sleep history

| Column | Type | Notes |
|---|---|---|
| `sleep_minutes_last_3h` | int | All strictly before `t`; a sleep straddling the boundary counts only its overlapping minutes |
| `sleep_minutes_last_6h` | int | |
| `sleep_minutes_last_12h` | int | |
| `sleep_minutes_last_24h` | int | |
| `sleep_count_last_24h` | int | Number of sleep onsets |
| `nap_count_today_so_far` | int | **"So far" is load-bearing** — not the full-day count from `mart_daily_metrics` |
| `nap_minutes_today_so_far` | int | Same caveat |
| `avg_nap_minutes_today_so_far` | float | Same caveat |
| `last_night_sleep_minutes` | int | Total sleep in the night that preceded today |
| `last_night_longest_stretch_minutes` | int | |
| `last_night_waking_count` | int | |
| `sleep_debt_24h_minutes` | float | `sleep_minutes_last_24h − (trailing 14-day median of 24h sleep for this baby)`. Negative = under-slept = more likely to crash |

### 4.5 Feeding

| Column | Type | Notes |
|---|---|---|
| `minutes_since_last_feed_end` | int | The "has she eaten recently" feature |
| `minutes_since_last_feed_start` | int | |
| `last_feed_duration_minutes` | int | You asked for "how many minutes she fed for" |
| `fed_in_last_30_mins` | bool | You asked for this one explicitly |
| `fed_in_last_60_mins` | bool | |
| `fed_in_last_15_mins` | bool | A feed-to-sleep transition is usually tighter than 30 min, so the shorter window may carry more signal |
| `last_feed_type` | text | `BREAST` / `BOTTLE`. **Low value — flagged.** Only 84 of 3,382 feeds are bottles across both babies, so this is ~97.5 % constant |
| `last_feed_breast_side` | text | `LEFT` / `RIGHT` / null |
| `feed_count_last_3h` | int | |
| `feed_count_last_6h` | int | |
| `feed_count_last_24h` | int | |
| `feed_minutes_last_3h` | int | Total minutes nursed |
| `feed_minutes_last_24h` | int | |
| `avg_feed_interval_last_24h` | float | |
| `is_cluster_feeding` | bool | ≥3 feeds in the last 3 hours. Cluster feeding is a distinct behavioural state and usually precedes a long sleep |

*Considered and rejected:* `feed_volume_ml_last_24h`. Volume is recorded only
for bottle/expressed feeds — 84 of 3,382 — so it's null for 97.5 % of
feeds. Not worth the column. Likewise `formula_type`, `food_items`, `appetite`:
the schema has them, the transform never populates them.

### 4.6 Diapers

| Column | Type | Notes |
|---|---|---|
| `minutes_since_last_diaper_change` | int | |
| `minutes_since_last_dirty_diaper` | int | |
| `had_dirty_diaper_last_60_mins` | bool | A poo tends to either precede settling or wreck it. Either way it's informative |
| `diaper_count_last_6h` | int | |
| `diaper_count_last_24h` | int | |
| `dirty_diaper_count_last_24h` | int | |

Caveat worth stating: a diaper *change* timestamp records when the parent
noticed, not when the event happened. These will be noisier than the sleep and
feed features, and I'd expect modest importance. Cheap enough to include and
find out.

### 4.7 Identity — carried, not modelled

| Column | Type | Notes |
|---|---|---|
| `baby_id` | uuid | Join key |
| `baby_name` | text | Grouping and splitting only. **Excluded from the feature matrix in Python** (§1.4) |

### 4.8 What we can't build, and what that costs us

Being honest about this now is better than discovering it during error analysis.

| Wanted | Why not |
|---|---|
| Sleep quality, location, sleep environment, wake reason | The columns exist in `raw_sleep_sessions`, but `transform_seeds.py` writes constants — `location='CRIB'`, `wake_reason='NATURAL'`, quality and environment empty. Zero variance |
| Solids, appetite, formula type | Never populated by the transform |
| **Illness, teething, temperature** | `health_events` has no seed data at all. **This is the biggest missing confounder** — a sick or teething baby sleeps completely differently, and we have no way to know. Some of the model's irreducible error will be this |
| Growth measurements | No seed data. Would have given a weight-percentile feature |
| Tummy time / other activity | `Ember_other_activity.csv` has 6 rows and isn't loaded into the database at all |
| Medication | `Imogen_medication.csv` has 1 row, not loaded |
| Weather, room temperature, who's minding her, car/pram motion, daycare | Never tracked. Motion in particular probably explains a chunk of the unpredictable naps |

### 4.9 Leakage traps specific to this dataset

Four things I want to write down before building, because each is easy to get
wrong and invisible once wrong:

1. **Do not join `mart_daily_metrics` for the current day.** It is right there
   and it is tempting and it would be a disaster. `nap_count` on that table is
   the count for the *whole* day, including naps that happen after `t`. Knowing
   she'll take 4 naps today tells you about her future. Every "today" feature
   in §4.4 must be recomputed as a strictly-before-`t` running total. Yesterday's
   row from that mart is safe; today's is not.
2. **Trailing baselines must be trailing.** `wake_window_vs_recent_median` and
   `sleep_debt_24h_minutes` divide by a per-baby median. If that median is
   computed over the whole dataset, test-set information flows into every
   training row. Use a rolling 14-day window ending strictly before `t`.
3. **The 6-hour gap exclusion is itself label-adjacent.** We drop rows inside
   suspiciously long awake gaps, and "long awake gap" correlates with "didn't
   fall asleep soon". Dropping them nudges the base rate up slightly. It's the
   right call — those rows are measurement error, not behaviour — but it should
   be stated, and I'll report the base rate with and without the exclusion.
4. **Grid rows are not independent.** Already covered in §1.2, but it bears
   repeating here because it's what makes row-level splitting so dangerous.

---

## 5. Why this doesn't blow up in size — the short answer

You asked how to avoid "accounting for every single time in the day". Three
things do it:

1. **We only materialise awake moments.** 62 % of a baby's life is asleep and
   generates no rows at all. A full-minute spine would be 726,000 rows; we have
   27,800 — a 26× reduction, and every row we keep is one we'd actually train on.
2. **10-minute resolution, not 1-minute.** The class balance is identical at
   every grid size I tested (29.5 % at 5, 10, 15 and 30 minutes), so the finer
   grids buy literally nothing but rows. Given the autocorrelation, 1-minute
   resolution would be 10× the storage for approximately zero extra information.
3. **Features are computed *at* the prediction points, not pre-aggregated
   everywhere.** There's no intermediate "every-minute state table". Each
   feature is a look-backward from the ~27,800 spine rows into the event tables.

**On the SQL that does the looking-backward.** The natural pattern is a lateral
join per feature family:

```sql
select
    p.*,
    round(extract(epoch from (p.prediction_time - s.end_time)) / 60)::int
        as minutes_since_last_wake
from {{ ref('ml_prediction_points') }} p
left join lateral (
    select end_time, duration_minutes, is_night
    from {{ ref('fct_sleep_sessions') }} s
    where s.baby_id = p.baby_id
      and s.end_time <= p.prediction_time
    order by s.end_time desc
    limit 1
) s on true
```

This is readable and each family is independently reviewable. At 27,800 spine
rows against ~2,800 sleeps per baby it should run in seconds, but it is a
nested loop and it won't stay fast forever. If it drags, the fallback is the
union-and-window pattern — UNION the spine and the events into one stream
ordered by time, then carry state forward with
`last_value(...) over (... rows between unbounded preceding and current row)`,
which is O(n log n) rather than O(n × m). I'd start with lateral joins for
clarity and only switch if the build is slow enough to be annoying.

Structurally, the feature table is built as one CTE per family (sleep state,
sleep history, feeding, diapers, calendar), each keyed on the spine, all joined
at the end on `(baby_id, prediction_time)`. Adding a feature means editing one
CTE. Same shape as `mart_daily_metrics` today, so it'll read familiarly.

---

## 6. Files this creates

Nothing existing is modified.

```
baby_data/models/ml/
├── ml_prediction_points.sql      the spine — grain + splits
├── ml_prediction_points.yml
├── ml_sleep_labels.sql           spine + 4 labels
├── ml_sleep_labels.yml
├── ml_sleep_features.sql         spine + ~55 features
├── ml_sleep_features.yml
├── ml_sleep_training_set.sql     the 1:1 join, materialised as a view
└── ml_sleep_training_set.yml

baby_data/tests/
├── assert_ml_tables_same_grain.sql
└── assert_no_prediction_point_during_sleep.sql
```

Plus a `models.baby_data.ml` block in `dbt_project.yml` (`+materialized: table`,
`+schema: ml`), mirroring the existing `marts` block.

Every `.yml` gets full column descriptions, in keeping with the existing models.

---

## 7. Phase 2 — the models (sketch only, not for approval yet)

Listed so you can check the data design won't block any of it.

**Where the code goes.** A new `baby_data/ml/` Python package in this repo, or
a separate repo — worth a conversation. Either way, structured as: a
`FeatureSpec` Pydantic model naming which columns are features vs metadata vs
labels (so the exclusion of `baby_name` is enforced in code, not remembered); a
thin loader that reads `ml.ml_sleep_training_set` into a DataFrame; and one
class per model family behind a common interface. That's a natural place to
work through the OO patterns you wanted to dig into — an abstract base class
with `fit`/`predict_proba`/`feature_importance`, four concrete subclasses, and
a runner that doesn't care which one it's holding. Small enough to be real,
big enough to show why the abstraction earns its place.

**Metrics.** PR-AUC as the headline — at a 29.5 % base rate, accuracy is
worthless and ROC-AUC flatters. Plus ROC-AUC, Brier score, and a calibration
curve, because "70 % likely to nap" should mean it happens 70 % of the time.

**Baselines to beat, in order.** (1) Always predict the base rate. (2) An
hour-of-day lookup table. (3) Single-feature logistic regression on
`minutes_since_last_wake`. If LightGBM doesn't clear baseline 3 by a decent
margin, the honest conclusion is that a parent's intuition plus a clock is
already most of the available signal — and that's a real finding, not a
failure.

**Then** the comparison against newer approaches you mentioned. The
`minutes_to_next_sleep` column is already in the label table so a survival model
(which predicts *when*, not just *whether*) can be dropped in without rebuilding
anything.

---

## 8. What I need from you

1. **Approve or edit the feature list in §4.** That's the main ask. Anything
   you know matters as a parent that I've missed? Anything I've listed that
   you know is noise?
2. **Confirm the onset-based label definition (§1.1).**
3. **Confirm the 10-minute grid over random sampling (§1.2)** — or tell me
   you'd rather have the random sample and I'll build it with a pinned seed.
4. **Confirm day-level splitting (§1.3).**
5. **Confirm the `ml` schema location (§1.5).**

Once you've said yes I'll build the four dbt models, the tests, and the `.yml`
docs, run `dbt build` against the real database, and come back with actual row
counts, the label distributions, and a first look at whether any feature is
accidentally constant.
