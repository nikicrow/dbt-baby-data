{#
  The training labels. Same grain as ml_prediction_points, one row each.

  ONSET-BASED, per review: the label is 1 if a sleep session STARTS within the
  window, not if she happens to be asleep at the far end of it. A 20-minute
  catnap beginning in 5 minutes is a yes, because the question the model
  answers is "is it worth starting to settle her now?".

  minutes_to_next_sleep is carried raw so a different horizon (45, 90) is a
  `case when` rather than a rebuild, and so a survival model — which predicts
  WHEN rather than WHETHER — can use this table unchanged.
#}

{{ config(materialized='table') }}

with points as (

    select * from {{ ref('ml_prediction_points') }}

),

with_next_sleep as (

    select
        p.*,
        next_sleep.block_start as next_sleep_start
    from points p
    -- LEFT is load-bearing: a plain `join lateral` would silently drop any row
    -- with no match, changing the grain and breaking the 1:1 join promise.
    -- (The spine already excludes points without 60 minutes of forward
    -- visibility, so in practice every row matches — the assert_ml_* tests
    -- prove it rather than assuming it.)
    left join lateral (
        select b.block_start
        from {{ ref('fct_sleep_blocks') }} b
        where b.baby_id = p.baby_id
          and b.block_start > p.prediction_time
        order by b.block_start
        limit 1
    ) next_sleep on true

)

select
    prediction_id,
    baby_id,
    baby_name,
    prediction_time,
    calendar_date,

    round(extract(epoch from (next_sleep_start - prediction_time)) / 60)::int
        as minutes_to_next_sleep,

    case when next_sleep_start <= prediction_time + interval '30 minutes'
         then 1 else 0 end as is_asleep_30_mins,
    case when next_sleep_start <= prediction_time + interval '60 minutes'
         then 1 else 0 end as is_asleep_60_mins,

    split_forward_time,
    split_random_day,
    sample_bucket

from with_next_sleep
