{#
  SLEEP feature family. One row per prediction point.

  Everything derivable from fct_sleep_blocks: current wake state, trailing
  sleep totals, last night, and the rolling per-baby baselines.

  Selects from ml_prediction_points, so it shares the spine's grain by
  construction and joins 1:1 to its sibling families on prediction_id. It is
  usable on its own — a model about night wakings or wake-window drift needs
  this table and nothing else.

  THE ONE RULE: every column uses only blocks at or before prediction_time,
  and the trailing baselines roll over a window ENDING before the prediction
  point rather than over the whole dataset.
#}

{{ config(materialized='table') }}

with points as (

    select * from {{ ref('ml_prediction_points') }}

),

blocks as (

    select * from {{ ref('fct_sleep_blocks') }}

),

wake_windows as (

    -- Completed awake gaps between consecutive blocks. Derived here rather
    -- than from fct_wake_windows, which is daytime-only and computes its gaps
    -- off raw unmerged sessions.
    select
        baby_id,
        block_end as wake_start,
        lead(block_start) over (partition by baby_id order by block_start) as wake_end,
        round(extract(epoch from (
            lead(block_start) over (partition by baby_id order by block_start) - block_end
        )) / 60)::int as wake_minutes
    from blocks

),

daily_sleep as (

    select
        baby_id,
        calendar_date,
        sum(block_duration_minutes)::int as daily_sleep_minutes
    from blocks
    group by 1, 2

),

enriched as (

    select
        p.prediction_id,
        p.baby_id,
        p.prediction_time,
        p.calendar_date,

        last_sleep.block_end as last_wake_time,
        last_sleep.block_start as last_sleep_start,
        last_sleep.block_duration_minutes as last_sleep_duration_minutes,
        last_sleep.is_night as last_sleep_was_night,
        last_night_end.block_end as morning_wake_time,
        naps_since_night.nap_count as nap_number_today,

        sleep_hist.sleep_minutes_last_3h,
        sleep_hist.sleep_minutes_last_6h,
        sleep_hist.sleep_minutes_last_12h,
        sleep_hist.sleep_minutes_last_24h,
        sleep_hist.sleep_count_last_24h,

        naps_today.nap_count_today_so_far,
        naps_today.nap_minutes_today_so_far,
        naps_today.avg_nap_minutes_today_so_far,

        last_night.last_night_sleep_minutes,
        last_night.last_night_longest_stretch_minutes,
        last_night.last_night_waking_count,

        ww_median.median_wake_window_14d,
        ww_recent.avg_wake_window_last_3,
        ds_median.median_daily_sleep_14d

    from points p

    -- The sleep she most recently woke from. Every spine row is an awake row,
    -- so this block is always strictly in the past.
    left join lateral (
        select b.block_start, b.block_end, b.block_duration_minutes, b.is_night
        from blocks b
        where b.baby_id = p.baby_id
          and b.block_end <= p.prediction_time
        order by b.block_end desc
        limit 1
    ) last_sleep on true

    -- End of the most recent NIGHT — positions her in the day's rhythm better
    -- than the clock does, because bedtimes and wake times drift.
    left join lateral (
        select b.block_end
        from blocks b
        where b.baby_id = p.baby_id
          and b.is_night
          and b.block_end <= p.prediction_time
        order by b.block_end desc
        limit 1
    ) last_night_end on true

    left join lateral (
        select count(*)::int as nap_count
        from blocks b
        where b.baby_id = p.baby_id
          and not b.is_night
          and b.block_end <= p.prediction_time
          and b.block_end > last_night_end.block_end
    ) naps_since_night on true

    -- Trailing totals. Each block is CLIPPED to the window, so a sleep
    -- straddling the boundary contributes only its overlapping minutes.
    left join lateral (
        select
            coalesce(sum(greatest(0, extract(epoch from (
                least(b.block_end, p.prediction_time)
                - greatest(b.block_start, p.prediction_time - interval '3 hours')
            )) / 60)), 0)::int as sleep_minutes_last_3h,
            coalesce(sum(greatest(0, extract(epoch from (
                least(b.block_end, p.prediction_time)
                - greatest(b.block_start, p.prediction_time - interval '6 hours')
            )) / 60)), 0)::int as sleep_minutes_last_6h,
            coalesce(sum(greatest(0, extract(epoch from (
                least(b.block_end, p.prediction_time)
                - greatest(b.block_start, p.prediction_time - interval '12 hours')
            )) / 60)), 0)::int as sleep_minutes_last_12h,
            coalesce(sum(greatest(0, extract(epoch from (
                least(b.block_end, p.prediction_time)
                - greatest(b.block_start, p.prediction_time - interval '24 hours')
            )) / 60)), 0)::int as sleep_minutes_last_24h,
            count(*) filter (
                where b.block_start > p.prediction_time - interval '24 hours'
            )::int as sleep_count_last_24h
        from blocks b
        where b.baby_id = p.baby_id
          and b.block_end > p.prediction_time - interval '24 hours'
          and b.block_start < p.prediction_time
    ) sleep_hist on true

    -- "So far today" — NOT the whole-day totals from mart_daily_metrics, which
    -- include naps that have not happened yet.
    left join lateral (
        select
            count(*)::int as nap_count_today_so_far,
            coalesce(sum(b.block_duration_minutes), 0)::int as nap_minutes_today_so_far,
            round(avg(b.block_duration_minutes))::int as avg_nap_minutes_today_so_far
        from blocks b
        where b.baby_id = p.baby_id
          and not b.is_night
          and b.calendar_date = p.calendar_date
          and b.block_end <= p.prediction_time
    ) naps_today on true

    -- The most recent night that has FINISHED. At 23:00 tonight has begun but
    -- is not complete, and its total would be future information.
    left join lateral (
        select b.night_date
        from blocks b
        where b.baby_id = p.baby_id
          and b.is_night
        group by b.night_date
        having max(b.block_end) <= p.prediction_time
        order by b.night_date desc
        limit 1
    ) ln_date on true

    left join lateral (
        select
            sum(b.block_duration_minutes)::int as last_night_sleep_minutes,
            max(b.block_duration_minutes)::int as last_night_longest_stretch_minutes,
            greatest(count(*) - 1, 0)::int as last_night_waking_count
        from blocks b
        where b.baby_id = p.baby_id
          and b.is_night
          and b.night_date = ln_date.night_date
    ) last_night on true

    left join lateral (
        select percentile_cont(0.5) within group (order by w.wake_minutes)
                   as median_wake_window_14d
        from wake_windows w
        where w.baby_id = p.baby_id
          and w.wake_end <= p.prediction_time
          and w.wake_end > p.prediction_time - interval '14 days'
          and w.wake_minutes between 5 and 360
    ) ww_median on true

    left join lateral (
        select avg(x.wake_minutes) as avg_wake_window_last_3
        from (
            select w.wake_minutes
            from wake_windows w
            where w.baby_id = p.baby_id
              and w.wake_end <= p.prediction_time
              and w.wake_minutes between 5 and 360
            order by w.wake_end desc
            limit 3
        ) x
    ) ww_recent on true

    left join lateral (
        select percentile_cont(0.5) within group (order by ds.daily_sleep_minutes)
                   as median_daily_sleep_14d
        from daily_sleep ds
        where ds.baby_id = p.baby_id
          and ds.calendar_date < p.calendar_date
          and ds.calendar_date >= p.calendar_date - 14
    ) ds_median on true

)

select
    prediction_id,
    baby_id,
    prediction_time,

    round(extract(epoch from (prediction_time - last_wake_time)) / 60)::int
        as minutes_since_last_wake,
    round(extract(epoch from (prediction_time - last_sleep_start)) / 60)::int
        as minutes_since_last_sleep_start,
    last_sleep_duration_minutes,
    last_sleep_was_night,
    round(extract(epoch from (prediction_time - morning_wake_time)) / 60)::int
        as minutes_since_morning_wake,
    nap_number_today,
    (nap_number_today = 0) as is_first_wake_of_day,
    case
        when median_wake_window_14d > 0 then round(
            (extract(epoch from (prediction_time - last_wake_time)) / 60
             / median_wake_window_14d)::numeric, 3)
    end as wake_window_vs_recent_median,
    round(avg_wake_window_last_3::numeric, 1) as avg_wake_window_last_3,

    sleep_minutes_last_3h,
    sleep_minutes_last_6h,
    sleep_minutes_last_12h,
    sleep_minutes_last_24h,
    sleep_count_last_24h,
    nap_count_today_so_far,
    nap_minutes_today_so_far,
    avg_nap_minutes_today_so_far,
    last_night_sleep_minutes,
    last_night_longest_stretch_minutes,
    last_night_waking_count,
    (sleep_minutes_last_24h - median_daily_sleep_14d)::numeric as sleep_debt_24h_minutes

from enriched
