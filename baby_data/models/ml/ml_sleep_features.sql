{#
  The feature matrix. Same grain as ml_prediction_points, one row each.

  THE ONE RULE: every feature uses only events at or before prediction_time.
  Every lateral below is bounded by `<= p.prediction_time`, and the trailing
  baselines (median wake window, median daily sleep) use rolling windows that
  END before the prediction point rather than statistics over the whole
  dataset, which would leak the test set into every training row.

  In particular NOTHING here reads mart_daily_metrics for the current day:
  its nap_count is a whole-day total including naps that happen after
  prediction_time. The "today so far" features are recomputed from scratch.

  One CTE/lateral per feature family. See the plan doc §4 for the full column
  list and the reasoning behind each.
#}

{{ config(materialized='table') }}

with points as (

    select * from {{ ref('ml_prediction_points') }}

),

blocks as (

    select * from {{ ref('fct_sleep_blocks') }}

),

feeds as (

    select * from {{ ref('stg_feeding_sessions') }}

),

diapers as (

    select * from {{ ref('stg_diaper_events') }}

),

wake_windows as (

    -- Completed awake gaps between consecutive sleep blocks. Derived here from
    -- fct_sleep_blocks rather than reusing fct_wake_windows, because that model
    -- is daytime-only and computes gaps off raw (unmerged) sessions.
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

    -- Total sleep per calendar date, for the trailing sleep-debt baseline.
    select
        baby_id,
        calendar_date,
        sum(block_duration_minutes)::int as daily_sleep_minutes
    from blocks
    group by 1, 2

),

enriched as (

    select
        p.*,

        -- ---------------------------------------------------------------
        -- (c) Current wake state
        -- ---------------------------------------------------------------
        last_sleep.block_end as last_wake_time,
        last_sleep.block_start as last_sleep_start,
        last_sleep.block_duration_minutes as last_sleep_duration_minutes,
        last_sleep.is_night as last_sleep_was_night,
        last_night_end.block_end as morning_wake_time,
        naps_since_night.nap_count as nap_number_today,

        -- ---------------------------------------------------------------
        -- (d) Recent sleep history
        -- ---------------------------------------------------------------
        sleep_hist.sleep_minutes_last_3h,
        sleep_hist.sleep_minutes_last_6h,
        sleep_hist.sleep_minutes_last_12h,
        sleep_hist.sleep_minutes_last_24h,
        sleep_hist.sleep_count_last_24h,
        naps_today.nap_count_today_so_far,
        naps_today.nap_minutes_today_so_far,
        naps_today.avg_nap_minutes_today_so_far,

        -- ---------------------------------------------------------------
        -- (e) Last COMPLETED night — see plan §4.4.1
        -- ---------------------------------------------------------------
        last_night.last_night_sleep_minutes,
        last_night.last_night_longest_stretch_minutes,
        last_night.last_night_waking_count,

        -- ---------------------------------------------------------------
        -- (h) Trailing per-baby baselines (rolling, never global)
        -- ---------------------------------------------------------------
        ww_median.median_wake_window_14d,
        ww_recent.avg_wake_window_last_3,
        ds_median.median_daily_sleep_14d,

        -- ---------------------------------------------------------------
        -- (f) Feeding
        -- ---------------------------------------------------------------
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
        feed_gap.avg_feed_interval_last_24h,

        -- ---------------------------------------------------------------
        -- (g) Diapers
        -- ---------------------------------------------------------------
        last_diaper.event_time as last_diaper_time,
        last_dirty.event_time as last_dirty_diaper_time,
        diaper_win.diaper_count_last_6h,
        diaper_win.diaper_count_last_24h,
        diaper_win.dirty_diaper_count_last_24h

    from points p

    -- The sleep she most recently woke from. All spine rows are awake rows, so
    -- this block is always strictly in the past.
    left join lateral (
        select b.block_start, b.block_end, b.block_duration_minutes, b.is_night
        from blocks b
        where b.baby_id = p.baby_id
          and b.block_end <= p.prediction_time
        order by b.block_end desc
        limit 1
    ) last_sleep on true

    -- End of the most recent NIGHT — positions her in the day's rhythm better
    -- than the clock, because bedtimes and wake times drift.
    left join lateral (
        select b.block_end
        from blocks b
        where b.baby_id = p.baby_id
          and b.is_night
          and b.block_end <= p.prediction_time
        order by b.block_end desc
        limit 1
    ) last_night_end on true

    -- Naps completed since that morning wake. The 3rd nap behaves differently
    -- from the 1st.
    left join lateral (
        select count(*)::int as nap_count
        from blocks b
        where b.baby_id = p.baby_id
          and not b.is_night
          and b.block_end <= p.prediction_time
          and b.block_end > last_night_end.block_end
    ) naps_since_night on true

    -- Trailing sleep totals. Each block is CLIPPED to the window so a sleep
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
    -- would include naps that have not happened yet.
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
    -- is not complete, and its total is future information.
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

    -- Rolling 14-day median wake window, for self-normalising away the age
    -- effect. Bounded 5-360 for the same reason fct_wake_windows is.
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

    -- Last COMPLETED feed. A feed in progress at prediction_time is excluded,
    -- because its end_time is in the future.
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
    -- Grouping and splitting always; as a FEATURE it is an open experiment
    -- (plan §1.4) toggled in FeatureSpec, not decided here.
    baby_name,
    prediction_time,

    -- ===============================================================
    -- 4.1 Time of day and calendar
    -- ===============================================================
    extract(hour from prediction_time)::int as hour_of_day,
    (extract(hour from prediction_time) * 60 + extract(minute from prediction_time))::int
        as minutes_since_midnight,
    -- Cyclic encoding: time of day is a circle, and logistic regression can
    -- only draw straight lines. Without these, 23:50 and 00:10 sit 1,420 units
    -- apart instead of 20. Trees do not need them and will ignore them.
    sin(2 * pi() * (extract(hour from prediction_time) * 60
        + extract(minute from prediction_time)) / 1440) as tod_sin,
    cos(2 * pi() * (extract(hour from prediction_time) * 60
        + extract(minute from prediction_time)) / 1440) as tod_cos,
    (extract(hour from prediction_time) >= 19
        or extract(hour from prediction_time) < 7) as is_night_hours,
    extract(dow from prediction_time)::int as day_of_week,
    (extract(dow from prediction_time) in (0, 6)) as is_weekend,

    -- ===============================================================
    -- 4.2 Age
    -- ===============================================================
    age_days,
    (age_days / 7)::int as age_weeks,
    round((age_days / 30.44)::numeric, 2) as age_months,

    -- ===============================================================
    -- 4.3 Current wake state
    -- ===============================================================
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

    -- ===============================================================
    -- 4.4 Recent sleep history
    -- ===============================================================
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
    (sleep_minutes_last_24h - median_daily_sleep_14d)::numeric as sleep_debt_24h_minutes,

    -- ===============================================================
    -- 4.5 Feeding
    -- ===============================================================
    round(extract(epoch from (prediction_time - last_feed_end)) / 60)::int
        as minutes_since_last_feed_end,
    round(extract(epoch from (prediction_time - last_feed_start)) / 60)::int
        as minutes_since_last_feed_start,
    last_feed_duration_minutes,
    (last_feed_end > prediction_time - interval '15 minutes') as fed_in_last_15_mins,
    (last_feed_end > prediction_time - interval '30 minutes') as fed_in_last_30_mins,
    (last_feed_end > prediction_time - interval '60 minutes') as fed_in_last_60_mins,
    last_feed_type::text as last_feed_type,
    -- breast_started is a Postgres enum; unset values are already NULL, so no
    -- empty-string handling is needed. Cast to text so pandas reads it as a
    -- plain categorical rather than an opaque enum.
    last_feed_breast_side::text as last_feed_breast_side,
    feed_count_last_3h,
    feed_count_last_6h,
    feed_count_last_24h,
    feed_minutes_last_3h,
    feed_minutes_last_24h,
    avg_feed_interval_last_24h,
    (feed_count_last_3h >= 3) as is_cluster_feeding,

    -- ===============================================================
    -- 4.6 Diapers
    -- ===============================================================
    round(extract(epoch from (prediction_time - last_diaper_time)) / 60)::int
        as minutes_since_last_diaper_change,
    round(extract(epoch from (prediction_time - last_dirty_diaper_time)) / 60)::int
        as minutes_since_last_dirty_diaper,
    (last_dirty_diaper_time > prediction_time - interval '60 minutes')
        as had_dirty_diaper_last_60_mins,
    diaper_count_last_6h,
    diaper_count_last_24h,
    dirty_diaper_count_last_24h

from enriched
