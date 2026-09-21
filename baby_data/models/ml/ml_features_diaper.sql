{#
  DIAPER feature family. One row per prediction point.

  Everything derivable from stg_diaper_events. Selects from
  ml_prediction_points, so it shares the spine's grain and joins 1:1 to its
  sibling families on prediction_id.

  Named `diaper` rather than `nappy` to match the rest of the project —
  stg_diaper_events, raw_diaper_events, the app's DiaperEvent model.

  Expect these to matter less than the sleep and feed features: a diaper
  timestamp records when the parent NOTICED, not when it happened, so they are
  structurally noisier.
#}

{{ config(materialized='table') }}

with points as (

    select * from {{ ref('ml_prediction_points') }}

),

diapers as (

    select * from {{ ref('stg_diaper_events') }}

),

enriched as (

    select
        p.prediction_id,
        p.baby_id,
        p.prediction_time,

        last_diaper.event_time as last_diaper_time,
        last_dirty.event_time as last_dirty_diaper_time,
        diaper_win.diaper_count_last_6h,
        diaper_win.diaper_count_last_24h,
        diaper_win.dirty_diaper_count_last_24h

    from points p

    left join lateral (
        select d.event_time
        from diapers d
        where d.baby_id = p.baby_id
          and d.event_time <= p.prediction_time
        order by d.event_time desc
        limit 1
    ) last_diaper on true

    left join lateral (
        select d.event_time
        from diapers d
        where d.baby_id = p.baby_id
          and d.has_stool
          and d.event_time <= p.prediction_time
        order by d.event_time desc
        limit 1
    ) last_dirty on true

    left join lateral (
        select
            count(*) filter (
                where d.event_time > p.prediction_time - interval '6 hours'
            )::int as diaper_count_last_6h,
            count(*)::int as diaper_count_last_24h,
            count(*) filter (where d.has_stool)::int as dirty_diaper_count_last_24h
        from diapers d
        where d.baby_id = p.baby_id
          and d.event_time <= p.prediction_time
          and d.event_time > p.prediction_time - interval '24 hours'
    ) diaper_win on true

)

select
    prediction_id,
    baby_id,
    prediction_time,

    round(extract(epoch from (prediction_time - last_diaper_time)) / 60)::int
        as minutes_since_last_diaper_change,
    round(extract(epoch from (prediction_time - last_dirty_diaper_time)) / 60)::int
        as minutes_since_last_dirty_diaper,
    -- A poo tends to either precede settling or wreck it. Either way it is
    -- informative.
    (last_dirty_diaper_time > prediction_time - interval '60 minutes')
        as had_dirty_diaper_last_60_mins,
    diaper_count_last_6h,
    diaper_count_last_24h,
    dirty_diaper_count_last_24h

from enriched
