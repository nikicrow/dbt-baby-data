# ML Sleep Prediction — Feature & Label Layer

**Status: BUILT.** Approved in review, models written, and `dbt build` green
against a real Postgres loaded from the committed seeds — **PASS=149,
ERROR=0**. Measured results in §8; §9 maps each review comment to its change.

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
2. **The tables are small.** 27k rows × 52 feature columns is a few megabytes. This
   answers your sizing worry directly — see §5.

And the signal is visibly there before we model anything. Probability she falls
asleep within 30 minutes, by hour of day (Ember): **15 % at 8 am, 16 % at 1 pm,
68 % at 5 am**. A model that only knew the clock would already beat the base
rate.

---

## 1. Design decisions — all approved in review

These are the five choices that shape everything else. I've made a
recommendation on each; overrule any of them.

### 1.1 "Asleep in the next 30 minutes" means *sleep onset* — **settled, onset-based**

`is_asleep_30_mins` = 1 if a sleep session **starts** at any point in the
window `(t, t+30min]`. Same for 60. That's it — the state-based alternative
(is she asleep at exactly t+30) is dropped, not carried as a spare column.

The two definitions disagree whenever she falls asleep at t+5 and wakes at
t+25: onset says 1, state says 0. Onset is the right one because it matches
the question you'd actually ask: *"is it worth starting to settle her now?"*
A 20-minute catnap starting in 5 minutes is still a yes.

`minutes_to_next_sleep` stays in the table regardless. It's the raw
time-to-event number both labels derive from, so any future label variant —
45 minutes, 90 minutes — is a `case when` away without rebuilding anything.

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

### 1.3 Split forward in time; never split by row

This is the single most important decision in the document. If we split rows
randomly, the row at 14:30 lands in train and the row at 14:40 lands in test,
and they are near-identical — the model looks brilliant and generalises to
nothing.

Two splits, both defined as columns on the label table so every model family
uses byte-identical data:

| Column | How | What it answers |
|---|---|---|
| **`split_forward_time`** | **Primary.** Per baby, chronological: first 70 % of calendar days `train`, next 15 % `val`, last 15 % `test` | "Does a model fit on data up to today still work next week?" |
| `split_random_day` | 70/15/15 assigned per `(baby_id, calendar_date)` by hash | Secondary benchmark. Grouped by day so no wake window straddles the boundary. Optimistic by comparison — useful mainly as the ceiling |

**Why forward-in-time is primary** — this changed after your "we won't have any
more babies" note, and it's the right change. With the roster closed at two,
the model is never going to meet a stranger. Ember's data is finished and
historical (it stops at 12 months, two years ago). The only live use is
**predicting for Imogen, going forward from today.** That is literally a
forward-in-time problem, so the split that mimics deployment should be the one
we judge on. A random-day split would let the model train on next Tuesday to
predict last Monday, which is not a thing it will ever get to do.

Test-set discipline: run the whole model bake-off against `val`, touch `test`
exactly once at the end.

**Dropped: the leave-one-baby-out split.** Per your note, not worth two schema
columns for two babies. Worth recording why it's no loss: "train on Ember,
test on Imogen" is one line of pandas (`df[df.baby_name == 'Ember']`) whenever
we're curious. The question it answers is no longer "will this generalise to a
stranger" but "is Ember's data actually helping predict Imogen, or is it just
noise from a different baby two years ago?" — which is worth 20 minutes at
model time, as an experiment rather than a schema commitment.

### 1.4 Keep calendar date out of the model; test `baby_name` both ways

Raw calendar date must stay out. With one baby per era, "October 2023" *is*
"Ember at 8 weeks", so a date feature lets the model identify both the baby and
her exact life stage from a single column, and it cannot possibly extrapolate
to 2026. `age_days` is the legitimate version of that information and stays;
`metric_date` does not.

**`baby_name` is a more interesting case now the roster is closed.** My
original reasoning — that it blocks generalising to a third baby — no longer
applies, since there is no third baby. What it becomes is a per-baby intercept:
"Imogen naps a bit more readily than Ember did," which is true (33.7 % vs
27.3 % base rate) and legitimately useful for the one baby we're predicting.
So: **train with it and without it, and keep whichever wins on `val`.** The
`FeatureSpec` in phase 2 makes this a one-line toggle. My guess is it barely
moves the needle, because `age_days` plus the trailing per-baby features
already carry most of what differs between them — but it's now an empirical
question rather than a rule.

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
       fct_sleep_blocks               ← NEW MART (§2.2). Overlapping sleep
       (continuous asleep intervals)     records merged into continuous
                │                        asleep intervals. Reusable, not
                ▼                        ML-specific.
              ml_prediction_points          ← THE SPINE. One row per
              (baby_id, prediction_time,       (baby, awake moment on the
               split_*, sample_bucket)         10-min grid). ~27,800 rows.
                     │        │                Defines the grain. Nothing
        ┌────────────┘        └────────────┐   else is allowed to change it.
        ▼                                  ▼
  ml_sleep_labels        ml_features_sleep ─┐   one model per TOPIC, each
  spine + 3 label cols   ml_features_feeding├─▶ selecting from the spine and
  27,235 rows            ml_features_diaper ┘   usable on its own
        │                                  │
        │                                  ▼
        │                         ml_sleep_features
        │                         spine + calendar/age + the 3 families
        │                         27,235 rows, 52 features
        │                                  │
        └──────────────┬───────────────────┘
                       ▼
            ml_sleep_training_set  (view)
            inner join on prediction_id
            27,235 rows — what Python reads
```

Four models. `ml_prediction_points` is the contract: both children select
`from ml_prediction_points` and add columns, so the join cannot fan out or drop
rows by construction.

**Guardrails, as dbt tests:**

- `unique` on `prediction_id` in all three tables — proves the grain in each.
- `not_null` on the key columns.
- A custom test `assert_ml_tables_same_grain.sql`: `ml_sleep_labels`,
  `ml_sleep_features` and `ml_prediction_points` all have identical row counts
  and no unmatched keys in either direction. This fails loudly the moment
  someone's `where` clause quietly drops rows from one side.
- A custom test asserting no prediction point falls inside a sleep session.
- `accepted_values` on `split_*` columns.

**Rough disk cost:** ~27,800 rows × ~60 numeric columns ≈ 13 MB per table,
under 50 MB for the layer. Rebuild time on Postgres should be seconds. The
dataset is closed at two babies, so this is the size it will ever be — the
only growth is Imogen's ongoing logging, roughly 59 rows a day.

**Why not one wide table?** Because the label definitions will change (you may
want a 45- or 90-minute horizon) far more often than the features
will, and vice versa. Separating them means a label change doesn't force a
recompute of 52 feature columns. The joined view costs nothing.

### 2.1 The surrogate key — `dbt_utils.generate_surrogate_key`

Taking your note: the key is built with the package macro rather than by
hand-concatenating strings.

```sql
{{ dbt_utils.generate_surrogate_key(['baby_id', 'prediction_time']) }}
    as prediction_id
```

It hashes the listed columns into one fixed-width md5 string. Three things it
gives us over `baby_id || '_' || prediction_time`:

- **Nulls don't collapse.** In Postgres, `'abc' || null` is `null`, so a
  hand-rolled key silently becomes null if any part is missing, and a `unique`
  test passes happily because Postgres doesn't compare nulls. The macro coerces
  each field to a string first, so a missing part produces a real (if odd) key
  that a `not_null` test will catch.
- **No separator ambiguity.** Concatenation can collide when values contain the
  separator. Not a live risk with a uuid and a timestamp, but it's free to not
  have to think about it.
- **Fixed width, and consistent with everywhere else** the pattern gets used
  later.

One housekeeping point: `dbt_utils` 1.3.3 is already resolved in
`package-lock.yml`, but only as a transitive dependency of `dbt-labs/codegen`
— it isn't in `packages.yml`. Since we'd now be calling its macros directly,
it should be declared explicitly, so a future `codegen` bump that drops the
dependency doesn't break our models:

```yaml
packages:
  - package: dbt-labs/codegen
    version: 0.12.1
  - package: dbt-labs/dbt_utils      # add — used directly by models/ml
    version: 1.3.3
```

### 2.2 `fct_sleep_blocks` — the merge logic, in a reusable layer

Taking your note that this belongs before the features so it can be recycled.
It becomes **its own mart model, `marts/fct_sleep_blocks.sql`** — not part of
the ML layer at all.

Putting it in `marts/` rather than `ml/` is deliberate: "when was she actually
asleep, continuously" is a general question about the data, not an ML concept.
The ML spine is then just one consumer of it.

**What it does.** `fct_sleep_sessions` has records that overlap — the same
sleep logged across two entries, 0.5 % of Ember's and 0.2 % of Imogen's. Left
alone, the seam between two overlapping records looks like a moment of
wakefulness that never happened. The model collapses any chain of sessions
where each starts at or before the previous one's end into a single continuous
block, per baby. The standard SQL for this is the "gaps and islands" pattern:
flag each row that starts a new island, cumulative-sum the flags into an island
id, then group by it.

**Grain:** one row per continuous asleep interval per baby. Roughly 1,725 rows
for Ember and 1,098 for Imogen, down from 1,734 and 1,100 sessions.

**Columns:** `sleep_block_id`, `baby_id`, `baby_name`, `block_start`,
`block_end`, `block_duration_minutes`, `session_count` (how many raw records
were merged — 1 for the vast majority), `is_night`, `night_date`, `age_days`,
`age_weeks`.

**Where else it's immediately useful**, which is the point of pulling it out:

- `fct_wake_windows` currently computes gaps from raw `stg_sleep_sessions`, so
  it has the same overlap problem — a merged-record seam can produce a spurious
  short wake window. Rebuilding it on `fct_sleep_blocks` would fix that. **I
  have not included that change here**, because it would shift numbers the app's
  Compare tab already displays, and that deserves to be its own decision rather
  than a side effect of an ML PR. Flagging it as a follow-up.
- "Longest continuous stretch" questions — the ones you actually care about at
  4 am — are a `max(block_duration_minutes)` on this table, and are currently
  slightly wrong anywhere they're computed from raw sessions.

### 2.3 One feature model per topic

Taking your review note: the features are not one monolith. Each event source
gets its own model, all at the spine's grain:

| Model | Source | Columns |
|---|---|---|
| `ml_features_sleep` | `fct_sleep_blocks` | 21 |
| `ml_features_feeding` | `stg_feeding_sessions` | 15 |
| `ml_features_diaper` | `stg_diaper_events` | 6 |
| `ml_sleep_features` | the three above, plus calendar/age | 52 |

Each family selects from `ml_prediction_points` and adds columns, so they all
share a grain by construction and join 1:1 on `prediction_id`. `ml_sleep_features`
is then a thin assembler.

What this buys, which is what you were after:

- **Adding a feature touches one model.** A new feeding feature rebuilds a
  15-column table, not a 52-column one.
- **A family is usable alone.** A model about feed timing, or about night
  wakings, can select the families it needs — the sleep family does not care
  that a sleep-onset model exists.
- **Failures localise.** A broken lateral in the diaper family fails
  `ml_features_diaper`, and the others still build.

**Why calendar and age are not a fourth family.** They have no event source —
they are pure functions of columns the spine already carries
(`prediction_time`, `age_days`). A separate model would add a join and a table
for ten columns that nothing else would ever reuse independently, so they live
on the assembler. Say the word if you'd rather have the symmetry.

**Naming:** `diaper`, not `nappy`, to match `stg_diaper_events`,
`raw_diaper_events` and the app's `DiaperEvent` model. Happy to switch if you
prefer your word over the schema's.

---

## 3. The label table — `ml_sleep_labels`

### 3.1 Grain

One row per `(baby_id, prediction_time)` where the baby was **awake** at
`prediction_time`, on a 10-minute grid.

### 3.2 Columns

| Column | Type | Meaning |
|---|---|---|
| `prediction_id` | text | `dbt_utils.generate_surrogate_key(['baby_id', 'prediction_time'])` — see §2.1 |
| `baby_id` | uuid | FK to `raw_baby_profiles` |
| `baby_name` | text | Grouping and splitting. As a *feature* it's an open experiment — §1.4 |
| `prediction_time` | timestamp | The moment we're standing at, local time |
| `is_asleep_30_mins` | int 0/1 | **Primary label.** A sleep session starts in `(t, t+30min]` |
| `is_asleep_60_mins` | int 0/1 | **Primary label.** A sleep session starts in `(t, t+60min]` |
| `minutes_to_next_sleep` | int | Raw time-to-event. Useful for diagnostics and for a survival-model comparison later |
| `split_random_day` | text | `train` / `val` / `test` |
| `split_forward_time` | text | `train` / `val` / `test` — **the primary split** |
| `sample_bucket` | int 0–99 | Deterministic hash, for reproducible subsampling |

### 3.3 How it's built

1. **Read merged sleep blocks** from `fct_sleep_blocks` (§2.2) — the new mart
   that collapses overlapping sleep records into continuous asleep intervals.
   The ML layer consumes it; it doesn't own it.
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
| `tod_cos` | float | `cos(...)`. Pairs with `tod_sin` — see §4.1.1 |
| `is_night_hours` | bool | 19:00–07:00, matching `fct_sleep_sessions.is_night` |
| `day_of_week` | int 0–6 | Picks up parental routine — weekend lie-ins, weekday outings |
| `is_weekend` | bool | |

All timestamps are naive local time (Australia/Sydney) as loaded — no timezone
conversion needed, but worth asserting rather than assuming, since the profile
table carries a `timezone` column that nothing currently reads.

#### 4.1.1 Why logistic regression needs `tod_sin` / `tod_cos` — your question

**The short version:** logistic regression can only draw straight lines, and
time of day is a circle. The sine/cosine pair turns the circle into two
straight-line-friendly numbers.

**The longer version.** Logistic regression computes one weight per feature and
adds everything up:

```
log-odds = w₀ + w₁ × minutes_since_midnight + w₂ × minutes_since_last_wake + …
```

Note what `w₁` can express: a *constant* effect per minute. One fixed number.
If `w₁` is positive, later is always sleepier, all day, without limit. If it's
negative, earlier is always sleepier. That is the only shape available.

Two separate things go wrong with raw `minutes_since_midnight`.

**1. The midnight seam.** The feature runs 0 to 1439 and then jumps back to 0.
So 23:50 (1430) and 00:10 (10) are 1,420 apart numerically, despite being 20
minutes apart in reality — and the model has no way to know. Ember's data makes
the cost concrete: she's roughly equally likely to fall asleep at 23:00 (35 %)
and 01:00 (43 %), but the model sees those as opposite extremes of the range
and is forced to give them wildly different predictions.

**2. Sleepiness isn't monotonic anyway.** From the hour-by-hour numbers in §0,
Ember's likelihood is low at 08:00 (15 %), rises to a bump around 14:00 (34 %),
drops again at 17:00 (18 %), and peaks overnight (68 % at 05:00). It goes up
and down. No single straight line fits that, no matter how you number the hours.

**The fix.** Place each time on a clock face and record its x and y coordinates:

```
tod_sin = sin(2π × minutes_since_midnight / 1440)
tod_cos = cos(2π × minutes_since_midnight / 1440)
```

| Time | minutes | `tod_sin` | `tod_cos` |
|---|---|---|---|
| 00:00 | 0 | 0.00 | 1.00 |
| 06:00 | 360 | 1.00 | 0.00 |
| 12:00 | 720 | 0.00 | −1.00 |
| 18:00 | 1080 | −1.00 | 0.00 |
| 23:50 | 1430 | −0.04 | 1.00 |

Look at 23:50 against 00:00 — nearly identical coordinates, which is correct;
they're 10 minutes apart. The seam is gone, because a circle has no seam. And
because there are now two features, the weighted sum
`w₁ × tod_sin + w₂ × tod_cos` can produce a smooth single-peaked curve over the
day and place the peak anywhere, rather than a straight line. Still one bump
rather than Ember's two, which is why `is_night_hours` is also in the list as a
step-change term — but far closer than a line.

**Why the trees don't care.** Random forest, XGBoost and LightGBM split on
thresholds — "is `minutes_since_midnight` < 420?" — and can stack as many
splits as they like, so they carve the day into arbitrary blocks and
reconstruct any shape, seam included. The columns are harmless there; the trees
will just ignore them if they're not useful.

This is exactly the kind of thing that makes the logistic-regression baseline
worth running properly rather than as a formality. If LightGBM beats a
*carelessly encoded* logistic regression, that tells you nothing about the
models — only that one of them was handed worse features.

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
| `last_night_sleep_minutes` | int | Total sleep in the last **completed** night — see §4.4.1 |
| `last_night_longest_stretch_minutes` | int | Longest single block within that night |
| `last_night_waking_count` | int | Gaps between sleep blocks within that night |
| `sleep_debt_24h_minutes` | float | `sleep_minutes_last_24h − (trailing 14-day median of 24h sleep for this baby)`. Negative = under-slept = more likely to crash |

#### 4.4.1 What counts as "night" — answering your question on `last_night_*`

**We reuse the definition this repo already has**, rather than inventing a
second one. `fct_sleep_sessions` classifies every session with `is_night`:

- starts between **19:00 and 07:00** → night, whatever the duration (so a
  3 am wake-and-resettle counts toward the night, not as a nap);
- starts between **18:00 and 19:00** → night only if longer than 3 hours
  (this exists because Ember's toddler bedtime often crept before 7 pm, but a
  short evening catnap should stay a nap);
- everything else → nap.

It also carries `night_date`, which attributes a night to the date it
**started** — so a 2 am block belongs to the previous calendar date. "The night
of 1 July" therefore means everything from 7 pm on the 1st through to the
morning of the 2nd.

Two reasons to reuse rather than redefine: `mart_daily_metrics` already reports
night sleep on this basis, so if the ML layer used its own definition, the
model and the app's Compare tab would disagree about the same night. And the
rule was tuned against these two babies' actual bedtimes.

**The subtlety is "last".** At 10 am, the last completed night is the one that
ended this morning. At 11 pm, tonight has already begun — but it is *not*
finished, and its total is exactly the kind of future information §4.9 forbids.
So the rule is: **`last_night_*` uses the most recent `night_date` whose final
sleep block ended at or before `t`.** At 11 pm that still points back to last
night, not tonight. Concretely:

| `prediction_time` | `last_night_*` refers to |
|---|---|
| 2 Jul, 10:00 | night of 1 Jul (ended ~06:40 on the 2nd) ✓ |
| 2 Jul, 18:00 | night of 1 Jul ✓ |
| 2 Jul, 23:00 | night of 1 Jul ✓ — *not* the in-progress night of 2 Jul |
| 3 Jul, 05:30 | night of 1 Jul — she is mid-night-waking, and the night of 2 Jul isn't done |

The last row is the awkward one: at 5:30 am the "last night" figure is over a
day stale. That's the price of not leaking. `sleep_minutes_last_12h` covers the
recent-past gap for those rows, which is partly why that family of trailing
windows is in the table.
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

### 4.7 Identity

| Column | Type | Notes |
|---|---|---|
| `baby_id` | uuid | Join key. Never a feature — it's a uuid, meaningless as a number |
| `baby_name` | text | Always used for grouping and splitting. Whether it's *also* a feature is the open experiment in §1.4 — a `FeatureSpec` toggle, tested both ways on `val` |

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

### 5.1 Where `lead()` beats the lateral — and where it doesn't

Review asked whether the spine's gap lookup should have been a window function
rather than two lateral joins, and whether that would be faster or more
correct. Measured, on the real data:

| Approach | Time |
|---|---|
| Anti-join + 2 laterals (original) | 8.2–11.2 s |
| `lead()` + range join | 4.1–4.5 s |

**`lead()` wins, and not only on speed.** The original asked, for each of 27,800
grid points, two separate "nearest block" questions. But the blocks are merged
and non-overlapping, so the gaps between them can be computed *once*, in a
single pass:

```sql
select baby_id,
       block_end as gap_start,
       lead(block_start) over (partition by baby_id order by block_start) as gap_end
from fct_sleep_blocks
```

Then a grid point is awake **if and only if** it falls inside one of those
gaps, so one range join replaces the anti-join *and* both laterals. That is
the bigger win: three operations become one, and "this point sits in a real
awake window" stops being a `where` clause bolted on afterwards and becomes
structural — the join cannot produce a null gap, so the null checks the old
`kept` filter needed are gone.

Verified byte-identical: zero rows differ in either direction across all ten
spine columns, and the whole build went from 1m46s to about 56s.

**Why it doesn't generalise to the rest of the layer.** The honest answer to
"should every lateral be a window function" is no, and the distinction is
worth stating because it is the thing to check when writing the next one:

- The spine's lookup is **interval containment** — each grid point lies inside
  exactly one gap, and a gap is a pair of adjacent rows. That is precisely
  what `lead()` produces, so the rewrite is natural.
- Most feature laterals are **aggregates over a trailing window** —
  `sum` of sleep in the last 24 hours, the median wake window over 14 days,
  feeds in the last 3 hours. `lag`/`lead` return the *adjacent row*, not a
  reduction over a range, so they cannot express these at all. A window frame
  (`range between interval '24 hours' preceding and current row`) could, but
  only after UNIONing the grid and the event stream into one ordered sequence,
  which trades a clear lateral for a much harder-to-read query.

For the record, `ml_features_sleep` is now the slow model at ~32 s of the ~56 s
build. I tested indexes on `(baby_id, block_end)` and friends as the cheaper
fix: only about 15 % better, because the laterals that dominate are the
aggregate ones, which scan a range whatever the index says — and dbt drops and
recreates the table each run anyway. Not worth the config, so not added.

#### What `left join lateral` is doing — your question

**In one line:** `LATERAL` lets a subquery in the `FROM` clause see the columns
of the row currently being processed, which a normal subquery cannot.

Compare. This is illegal in Postgres:

```sql
select p.*, s.end_time
from ml_prediction_points p
left join (
    select end_time from fct_sleep_blocks
    where baby_id = p.baby_id            -- ✗ ERROR: p is not visible here
    order by block_end desc limit 1
) s on true
```

A plain subquery in `FROM` is evaluated once, standalone, before any joining
happens. It has no idea `p` exists. Add the word `lateral` and that changes:

```sql
left join lateral (
    select end_time from fct_sleep_blocks
    where baby_id = p.baby_id            -- ✓ now legal
      and block_end <= p.prediction_time
    order by block_end desc limit 1
) s on true
```

Now the subquery runs **once per row of `p`**, with that row's values
substituted in. Conceptually it's a `for` loop:

```python
for p_row in prediction_points:          # 27,800 times
    s = (sleep_blocks
         .filter(baby_id == p_row.baby_id, block_end <= p_row.prediction_time)
         .sort(block_end, desc=True)
         .first())                       # or None
    yield {**p_row, "end_time": s.end_time if s else None}
```

Three details worth knowing:

- **`on true`** — a normal join needs a condition, but the correlation is
  already expressed inside the subquery's `where`, so there's nothing left to
  join on. `on true` means "keep whatever the subquery returned for this row".
  It's boilerplate; read it as punctuation.
- **`left` vs plain `join`** — if a row has no matching sleep block (the very
  first prediction point for a baby, with no history behind it), a plain
  `join lateral` would **drop that row entirely**, silently changing the grain
  and breaking the 1:1 join promise in §2. `left join lateral` keeps the row
  and fills nulls. For this design that word is load-bearing, not stylistic.
- **Why not a correlated scalar subquery in the `SELECT`?** For one column
  they're equivalent. But `limit 1` gives us the whole matching row at once, so
  a single lateral yields `end_time`, `block_duration_minutes` and `is_night`
  together. The scalar-subquery form needs a separate pass per column.

This is the SQL answer to "give me the most recent thing before now", which is
most of §4 — last wake, last feed, last diaper. Some databases have a dedicated
`ASOF JOIN` for it; Postgres doesn't, so lateral is the idiom.

At 27,800 spine rows against ~2,800 sleep blocks per baby it should run in
seconds, but it is a nested loop and it won't stay fast forever. If it drags, the fallback is the
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

## 6. Files this adds

Nothing existing is modified.

```
baby_data/models/marts/
├── fct_sleep_blocks.sql          NEW — merged continuous sleep intervals (§2.2)
└── fct_sleep_blocks.yml             general-purpose, not ML-specific

baby_data/models/ml/
├── ml_prediction_points.sql      the spine — grain + splits
├── ml_prediction_points.yml
├── ml_sleep_labels.sql           spine + 3 label columns
├── ml_sleep_labels.yml
├── ml_features_sleep.sql         topic family: fct_sleep_blocks
├── ml_features_sleep.yml
├── ml_features_feeding.sql       topic family: stg_feeding_sessions
├── ml_features_feeding.yml
├── ml_features_diaper.sql        topic family: stg_diaper_events
├── ml_features_diaper.yml
├── ml_sleep_features.sql         assembler: calendar/age + the 3 families
├── ml_sleep_features.yml
├── ml_sleep_training_set.sql     the 1:1 join, materialised as a view
└── ml_sleep_training_set.yml

baby_data/tests/
├── assert_ml_tables_same_grain.sql
└── assert_no_prediction_point_during_sleep.sql
```

Two config edits: a `models.baby_data.ml` block in `dbt_project.yml`
(`+materialized: table`, `+schema: ml`) mirroring the existing `marts` block,
and `dbt-labs/dbt_utils` declared explicitly in `packages.yml` (§2.1).

`fct_sleep_blocks` lands in `marts/` and so inherits `+materialized: table`
and `+schema: marts` — meaning it appears in the schema the app reads. It is
purely additive; nothing existing changes shape.

Every `.yml` gets full column descriptions, in keeping with the existing models.

---

## 7. Phase 2 — the models (sketch only, not for approval yet)

Listed so you can check the data design won't block any of it.

**Where the code goes.** A new `baby_data/ml/` Python package in this repo, or
a separate repo — worth a conversation. Either way, structured as: a
`FeatureSpec` Pydantic model naming which columns are features vs metadata vs
labels (which is also where the `baby_name` on/off experiment from §1.4 lives,
as a field rather than something to remember); a
thin loader that reads `ml.ml_sleep_training_set` into a DataFrame; and one
class per model family behind a common interface. That's a natural place to
work through the OO patterns you wanted to dig into — an abstract base class
with `fit`/`predict_proba`/`feature_importance`, four concrete subclasses, and
a runner that doesn't care which one it's holding. Small enough to be real,
big enough to show why the abstraction earns its place.

**Metrics.** PR-AUC as the headline, on `split_forward_time` — at a 29.5 %
base rate, accuracy is worthless and ROC-AUC flatters. Plus ROC-AUC, Brier score, and a calibration
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

## 8. Built — what actually came out

`dbt build` on a local Postgres 16 loaded the same way CI does (replay
`ci/source_schema.sql`, run `run_pipeline.py` over the committed seeds, 9,764
rows). Full run: **PASS=149, ERROR=0**, about 1m45s, of which the feature table
is roughly 60s.

### The tables match what the plan predicted

| | Predicted | Actual |
|---|---|---|
| Rows in the ML tables | ~27,270 | **27,235** |
| `is_asleep_30_mins` = 1 | 29.5 % | **29.9 %** |
| `is_asleep_60_mins` = 1 | 54.4 % | **55.1 %** |
| Merged sleep blocks, Ember | 1,725 | **1,725** (8 merges) |
| Merged sleep blocks, Imogen | 1,098 | **1,098** (2 merges) |

The base rates coming in slightly high is the §4.9 point 3 effect, as expected:
excluding rows inside 6-hour tracking gaps removes rows that were
disproportionately "didn't fall asleep soon".

The hour-of-day signal survives the pipeline intact. Against the independent
Python estimate from §0 (Ember): 05:00 **71 %** vs 68 % predicted, 08:00
**14 %** vs 15 %, 13:00 **16 %** vs 16 %, 17:00 **18 %** vs 18 %. Two separate
implementations agreeing this closely is good evidence the SQL does what was
intended.

### Invariants checked, all clean

All zero: negative `minutes_since_last_wake`, wake windows over 360 minutes,
negative time-since-feed or -diaper, non-positive or null
`minutes_to_next_sleep`, and rows where `is_asleep_30_mins = 1` but
`is_asleep_60_mins = 0`. The joined view returns exactly 27,235 rows with
27,235 distinct keys, so the 1:1 join holds. No feature is constant — the
minimum distinct count across all 55 is 2, which is correct for the booleans.

### ⚠️ Finding: the base rate drifts hard across the forward-time split

This was not in the plan and it matters for phase 2.

| Split | Rows | Ember ages | Imogen ages | `is_asleep_30_mins` = 1 |
|---|---|---|---|---|
| `train` | 19,119 | 3–37 wk | 0–16 wk | **33.3 %** |
| `val` | 4,072 | 37–44 wk | 16–20 wk | **22.9 %** |
| `test` | 4,044 | 44–52 wk | 20–23 wk | **21.0 %** |

The cause is clean and monotonic — forward in time is forward in age, and older
babies nap less readily at any given awake moment:

| Age | 0–4 wk | 8–12 wk | 16–20 wk | 28–32 wk | 40–44 wk | 48–52 wk |
|---|---|---|---|---|---|---|
| `is_asleep_30_mins` = 1 | 41.6 % | 38.3 % | 31.4 % | 24.9 % | 17.2 % | 18.5 % |

Ember's mean wake window stretches from 60 minutes in `train` to 91 in `test`.

This is the deployment reality rather than a bug — but three consequences:

1. **A model fit on `train` will be systematically over-confident on `test`.**
   Calibration is not optional here; report the Brier score and a calibration
   curve, not just ranking metrics.
2. **PR-AUC is not comparable across splits**, because its baseline *is* the
   base rate. A PR-AUC of 0.40 on `test` (base 21 %) is a better model than
   0.40 on `train` (base 33 %). Always quote the base rate alongside it.
3. `age_days` is doing heavy lifting, so the leave-one-baby-out experiment from
   §1.3 is more interesting than it first looked: Ember's `train` ages
   (3–37 wk) barely overlap Imogen's (0–16 wk).

### Three columns are nullable, deliberately

Everything else is `not_null` tested. These need an explicit imputation
decision in Python rather than a silent `fillna(0)`:

| Column | Null rows | Why |
|---|---|---|
| `avg_nap_minutes_today_so_far` | 6,522 (24 %) | No nap has finished yet today. Null means "no naps yet", which is **not** the same as a nap of length zero |
| `last_feed_breast_side` | 4,929 (18 %) | Bottle feeds, and breast feeds logged without a side |
| `avg_feed_interval_last_24h` | 100 (0.4 %) | Fewer than two feeds in the window, so there is no gap to average |

### Two notes on the build

- **`fct_sleep_blocks` confirms the overlap problem is real**: 8 blocks for
  Ember and 2 for Imogen merged more than one raw session, exactly matching the
  overlap counts in §0. Those are the seams that would otherwise read as
  spurious wake windows — the `fct_wake_windows` follow-up in §9 is a real bug,
  not a theoretical one.
- **`package-lock.yml` is left untouched.** Adding `dbt_utils` to
  `packages.yml` changes the lock's content hash, and `dbt deps` re-resolves
  and rewrites the lock automatically when it mismatches. Worth knowing: the
  committed hash *already* mismatches what the current dbt version computes, so
  CI has been re-locking on every run regardless of this change.

---

## 9. Review status

### Settled in review round 1 (PR #18)

| Your note | Change |
|---|---|
| "I prefer onset based" | §1.1 — onset only; the state-based columns are gone |
| "delete and the 60 mins version" | Both `is_asleep_at_*` columns dropped |
| "yeah we won't have any more babies" | §1.3 — **`split_forward_time` is now the primary split**, since the only live use is predicting Imogen going forward. §1.4 reopened: `baby_name` is now an experiment, not a ban. §2 sizing note no longer talks about scaling |
| "lets not bother with this, I only have two babies" | `split_holdout_baby` / `_rev` dropped; kept as a one-line pandas experiment, not schema |
| "use dbt surrogate key for this" | §2.1 — `dbt_utils.generate_surrogate_key`, plus declaring `dbt_utils` explicitly in `packages.yml` |
| "do this in a layer before the features so we can recycle the logic" | §2.2 — merge logic promoted to its own mart, `fct_sleep_blocks`, in `marts/` not `ml/` |
| "why does logistic regression need it?" | §4.1.1 — worked explanation |
| "how are we thinking to define night sleep?" | §4.4.1 — reuses the repo's existing `is_night` / `night_date`, with the "last completed night" rule spelled out |
| "can you explain a lateral left join" | §5 — worked explanation |

### Review round 3 (PR #18, on the code)

| Your note | Change |
|---|---|
| "materialise these into 3 separate feature tables first by topic" | §2.3 — `ml_features_sleep` / `_feeding` / `_diaper`, with `ml_sleep_features` reduced to an assembler. Verified column-for-column identical output: same 27,235 rows, same 56 columns, same base rates |
| "why limit 1" | Answered in `ml_sleep_labels.sql`: without it the lateral matches *every* future sleep and fans the table out; ordering ascending and keeping one picks the soonest, which is the only one the label depends on |
| "what is on true" | Answered in the same comment block: punctuation, since the correlation already lives in the subquery's `where` |
| "can you explain this" (the md5 split expression) | Answered inline in `ml_prediction_points.sql`, read inside out, including why it is hashed rather than `random()` and why 7 hex chars rather than 8 |
| "why lateral rather than lag/lead? more efficient? more correct?" | **You were right.** §5.1 — the spine's anti-join and both laterals are replaced by one `lead()` plus a range join. Output byte-identical, the model 2x faster, the whole build 40% faster |

One correction that fell out of the refactor: the feature count is **52**, not
the 55 quoted in earlier drafts. Nothing was lost — the original table also had
56 columns (52 features + 4 identity) — the earlier number was just wrong.

### Approved in review round 2

The 10-minute grid (§1.2) and the feature list (§4) were both signed off
without changes, which is what unblocked the build in §9.

### Still open

1. **§2.2 raises one follow-up I did not action:** `fct_wake_windows` has the
   same overlapping-record problem that `fct_sleep_blocks` fixes, so some of
   its short wake windows are artefacts. Rebuilding it on the new mart would
   change numbers the app's Compare tab already shows, so it wants its own PR.
   Want me to open an issue for it?

2. **Phase 2 (§7) has not started** — the Python package, `FeatureSpec`, and
   the four model families. The data layer is done and green; say the word.
