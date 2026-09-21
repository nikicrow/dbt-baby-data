{#
  FEEDING feature family. One row per prediction point.

  Everything derivable from stg_feeding_sessions. Selects from
  ml_prediction_points, so it shares the spine's grain and joins 1:1 to its
  sibling families on prediction_id.

  THE ONE RULE: every column uses only feeds that had ENDED at or before
  prediction_time. A feed in progress is excluded — its end_time is in the
  future, and using it would leak.
#}

{{ config(materialized='table') }}

with points as (

    select * from {{ ref('ml_prediction_points') }}

),

feeds as (

    select * from {{ ref('stg_feeding_sessions') }}

),

enriched as (

    select
        p.prediction_id,
        p.baby_id,
        p.prediction_time,

        last_feed.end_time as last_feed_end,
        last_feed.start_time as last_feed_start,
        last_feed.duration_minutes as last_feed_duration_minutes,
        last_feed.feeding_type as last_feed_type,
        last_feed.breast_started as last_feed_breast_side,

        feed_win.feed_count_last_3h,
        feed_win.feed_count_last_6h,
        feed_win.feed_count_last_24h,
        feed_win.feed_minutes_last_3h,
        feed_win.feed_minutes_last_24h,
        feed_gap.avg_feed_interval_last_24h

    from points p

    left join lateral (
        select f.start_time, f.end_time, f.duration_minutes, f.feeding_type, f.breast_started
        from feeds f
        where f.baby_id = p.baby_id
          and f.end_time <= p.prediction_time
        order by f.end_time desc
        limit 1
    ) last_feed on true

    left join lateral (
        select
            count(*) filter (
                where f.start_time > p.prediction_time - interval '3 hours'
            )::int as feed_count_last_3h,
            count(*) filter (
                where f.start_time > p.prediction_time - interval '6 hours'
            )::int as feed_count_last_6h,
            count(*)::int as feed_count_last_24h,
            coalesce(sum(f.duration_minutes) filter (
                where f.start_time > p.prediction_time - interval '3 hours'
            ), 0)::int as feed_minutes_last_3h,
            coalesce(sum(f.duration_minutes), 0)::int as feed_minutes_last_24h
        from feeds f
        where f.baby_id = p.baby_id
          and f.end_time <= p.prediction_time
          and f.start_time > p.prediction_time - interval '24 hours'
    ) feed_win on true

    left join lateral (
        select round(avg(g.gap_minutes))::int as avg_feed_interval_last_24h
        from (
            select extract(epoch from (
                f.start_time - lag(f.start_time) over (order by f.start_time)
            )) / 60 as gap_minutes
            from feeds f
            where f.baby_id = p.baby_id
              and f.end_time <= p.prediction_time
              and f.start_time > p.prediction_time - interval '24 hours'
        ) g
        where g.gap_minutes is not null
    ) feed_gap on true

)

select
    prediction_id,
    baby_id,
    prediction_time,

    round(extract(epoch from (prediction_time - last_feed_end)) / 60)::int
        as minutes_since_last_feed_end,
    round(extract(epoch from (prediction_time - last_feed_start)) / 60)::int
        as minutes_since_last_feed_start,
    last_feed_duration_minutes,
    -- A feed-to-sleep transition is usually tighter than 30 minutes, so the
    -- 15-minute flag may carry more signal than the one that was asked for.
    (last_feed_end > prediction_time - interval '15 minutes') as fed_in_last_15_mins,
    (last_feed_end > prediction_time - interval '30 minutes') as fed_in_last_30_mins,
    (last_feed_end > prediction_time - interval '60 minutes') as fed_in_last_60_mins,
    last_feed_type::text as last_feed_type,
    -- breast_started is a Postgres enum; unset values are already NULL. Cast
    -- to text so pandas reads it as a plain categorical, not an opaque enum.
    last_feed_breast_side::text as last_feed_breast_side,
    feed_count_last_3h,
    feed_count_last_6h,
    feed_count_last_24h,
    feed_minutes_last_3h,
    feed_minutes_last_24h,
    avg_feed_interval_last_24h,
    -- Cluster feeding is a distinct behavioural state and often precedes a
    -- long sleep.
    (feed_count_last_3h >= 3) as is_cluster_feeding

from enriched
