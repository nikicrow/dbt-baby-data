{#
  THE SPINE for the ML layer. One row per (baby, awake moment) on a 10-minute
  grid. Everything downstream selects from here and adds columns, so the label
  and feature tables cannot drift apart in grain.

  Only AWAKE moments exist here — a prediction is meaningless while she is
  already asleep, and ~62% of a baby's life is asleep, so materialising the
  full grid would be 26x the rows for nothing.

  Four exclusions, each of which would otherwise inject a row we cannot label
  honestly. See claude_plans/ML_SLEEP_PREDICTION_PLAN.md §3.4.
#}

{{ config(materialized='table') }}

with blocks as (

    select * from {{ ref('fct_sleep_blocks') }}

),

bounds as (

    select
        baby_id,
        baby_name,
        min(block_start) as first_log,
        max(block_end) as last_log
    from blocks
    group by 1, 2

),

profiles as (

    select id as baby_id, date_of_birth
    from {{ ref('raw_baby_profiles') }}

),

grid as (

    -- Aligned to :00/:10/:20/... by truncating the start to the hour, so the
    -- grid is identical on every rebuild and comparable across babies.
    select
        b.baby_id,
        b.baby_name,
        b.first_log,
        b.last_log,
        gs as prediction_time
    from bounds b
    cross join lateral generate_series(
        date_trunc('hour', b.first_log),
        b.last_log,
        interval '10 minutes'
    ) as gs

),

awake as (

    select g.*
    from grid g
    where not exists (
        select 1
        from blocks b
        where b.baby_id = g.baby_id
          and g.prediction_time >= b.block_start
          and g.prediction_time <  b.block_end
    )

),

with_gap as (

    -- The awake gap this point sits inside: the previous block to end and the
    -- next block to start. A gap longer than 6 hours is a tracking hole, not a
    -- genuinely awake baby, and the rows inside it are measurement error.
    -- 6 hours matches the bound fct_wake_windows already uses.
    select
        a.*,
        prev_block.block_end as gap_start,
        next_block.block_start as gap_end,
        round(extract(epoch from (next_block.block_start - prev_block.block_end)) / 60)::int
            as enclosing_gap_minutes
    from awake a
    left join lateral (
        select b.block_end
        from blocks b
        where b.baby_id = a.baby_id
          and b.block_end <= a.prediction_time
        order by b.block_end desc
        limit 1
    ) prev_block on true
    left join lateral (
        select b.block_start
        from blocks b
        where b.baby_id = a.baby_id
          and b.block_start > a.prediction_time
        order by b.block_start
        limit 1
    ) next_block on true

),

kept as (

    select *
    from with_gap
    where
        -- Need 60 minutes of forward visibility to know the label. Labelling
        -- these 0 would be inventing negatives.
        prediction_time <= last_log - interval '60 minutes'
        -- No history, so every trailing feature would be null.
        and prediction_time >= first_log + interval '24 hours'
        -- Inside a tracking hole.
        and gap_start is not null
        and gap_end is not null
        and enclosing_gap_minutes <= 360

),

day_rank as (

    -- Chronological position of each calendar date within that baby's range,
    -- for the forward-in-time split. percent_rank is 0 for the first date and
    -- 1 for the last.
    select
        baby_id,
        calendar_date,
        percent_rank() over (partition by baby_id order by calendar_date) as day_percentile
    from (
        select distinct baby_id, prediction_time::date as calendar_date
        from kept
    ) d

)

select
    {{ dbt_utils.generate_surrogate_key(['k.baby_id', 'k.prediction_time']) }} as prediction_id,
    k.baby_id,
    k.baby_name,
    k.prediction_time,
    k.prediction_time::date as calendar_date,
    k.prediction_time::date - p.date_of_birth as age_days,
    k.enclosing_gap_minutes,

    -- PRIMARY SPLIT. Chronological per baby: the only live use of this model is
    -- predicting Imogen forward from today, so the evaluation should mimic that
    -- rather than letting the model train on next week to predict last week.
    case
        when dr.day_percentile < 0.70 then 'train'
        when dr.day_percentile < 0.85 then 'val'
        else 'test'
    end as split_forward_time,

    -- Secondary benchmark. Grouped by DAY, never by row: rows 10 minutes apart
    -- are near-duplicates, so a row-level split leaks badly.
    --
    -- The md5 expression is a deterministic stand-in for random(), read
    -- inside out:
    --   md5(baby_id || '|' || date)  -> a 32-char hex string, stable forever
    --   substr(..., 1, 7)            -> its first 7 hex chars
    --   'x' || ...                   -> makes that a hex BIT-STRING literal
    --   ::bit(28)::int               -> 28 bits as an integer, 0 .. 268435455
    --   % 100                        -> a bucket 0-99, evenly spread
    --
    -- Why not random()? Because a rebuild would reshuffle every row into a
    -- different split, so yesterday's model scores would not be comparable to
    -- today's. Hashing the key means the assignment is a pure function of the
    -- data: same baby, same date, same bucket, on any machine, forever.
    --
    -- Why 7 chars and not 8? 8 hex chars is bit(32), which casts to a SIGNED
    -- int and can come out negative, making `% 100` return a negative bucket.
    -- 7 chars maxes out at 2^28-1, comfortably positive. This is the standard
    -- Postgres idiom for exactly that reason.
    case
        when ('x' || substr(md5(k.baby_id::text || '|' || k.prediction_time::date::text), 1, 7))::bit(28)::int % 100 < 70 then 'train'
        when ('x' || substr(md5(k.baby_id::text || '|' || k.prediction_time::date::text), 1, 7))::bit(28)::int % 100 < 85 then 'val'
        else 'test'
    end as split_random_day,

    -- Deterministic 0-99 bucket, so any reproducible subsample is one
    -- `where sample_bucket < n` away without a seed to thread through.
    ('x' || substr(md5(k.baby_id::text || '|' || k.prediction_time::text), 1, 7))::bit(28)::int % 100
        as sample_bucket

from kept k
inner join profiles p on p.baby_id = k.baby_id
left join day_rank dr
    on dr.baby_id = k.baby_id
   and dr.calendar_date = k.prediction_time::date
