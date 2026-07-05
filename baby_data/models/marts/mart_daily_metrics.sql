{#
  One row per baby per day — the table the frontend's Compare tab reads.
  Join Ember and Imogen on age_days/age_weeks for age-aligned comparison.

  Night metrics are attributed to the date the night STARTED, so
  night_sleep_minutes on 2026-07-01 means "the night of 1 July",
  including segments that ran past midnight into 2 July.
#}

with profiles as (

    select
        id as baby_id,
        name as baby_name,
        date_of_birth
    from {{ ref('raw_baby_profiles') }}

),

event_dates as (

    select baby_id, start_time::date as event_date from {{ ref('stg_feeding_sessions') }}
    union all
    select baby_id, start_time::date from {{ ref('stg_sleep_sessions') }}
    union all
    select baby_id, event_time::date from {{ ref('stg_diaper_events') }}

),

bounds as (

    select
        baby_id,
        min(event_date) as first_date,
        max(event_date) as last_date
    from event_dates
    group by baby_id

),

date_spine as (

    select
        p.baby_id,
        p.baby_name,
        p.date_of_birth,
        gs::date as metric_date
    from profiles p
    inner join bounds b on b.baby_id = p.baby_id
    cross join lateral generate_series(b.first_date, b.last_date, interval '1 day') as gs

),

night_sleep as (

    select
        baby_id,
        night_date as metric_date,
        sum(duration_minutes) as night_sleep_minutes,
        count(*) as night_sleep_segments,
        max(duration_minutes) as longest_night_stretch_minutes
    from {{ ref('fct_sleep_sessions') }}
    where is_night
    group by 1, 2

),

naps as (

    select
        baby_id,
        calendar_date as metric_date,
        count(*) as nap_count,
        sum(duration_minutes) as total_nap_minutes,
        round(avg(duration_minutes))::int as avg_nap_minutes
    from {{ ref('fct_sleep_sessions') }}
    where not is_night
    group by 1, 2

),

feeds as (

    select
        baby_id,
        start_time::date as metric_date,
        count(*) as feed_count,
        count(*) filter (where feeding_type = 'BREAST') as breast_feed_count,
        count(*) filter (where feeding_type = 'BOTTLE') as bottle_feed_count,
        sum(volume_consumed_ml) as total_volume_ml
    from {{ ref('stg_feeding_sessions') }}
    group by 1, 2

),

feed_gaps as (

    select
        baby_id,
        start_time::date as metric_date,
        extract(epoch from (
            start_time - lag(start_time) over (partition by baby_id order by start_time)
        )) / 60 as gap_minutes
    from {{ ref('stg_feeding_sessions') }}

),

feed_intervals as (

    select
        baby_id,
        metric_date,
        round(avg(gap_minutes))::int as avg_feed_interval_minutes
    from feed_gaps
    where gap_minutes between 1 and 720
    group by 1, 2

),

wake_windows as (

    select
        baby_id,
        calendar_date as metric_date,
        round(avg(wake_window_minutes))::int as avg_wake_window_minutes,
        max(wake_window_minutes) as max_wake_window_minutes
    from {{ ref('fct_wake_windows') }}
    group by 1, 2

),

nappies as (

    select
        baby_id,
        event_time::date as metric_date,
        count(*) as diaper_count,
        count(*) filter (where has_urine) as wet_diaper_count,
        count(*) filter (where has_stool) as dirty_diaper_count
    from {{ ref('stg_diaper_events') }}
    group by 1, 2

)

select
    d.baby_id,
    d.baby_name,
    d.metric_date,
    d.metric_date - d.date_of_birth as age_days,
    (d.metric_date - d.date_of_birth) / 7 as age_weeks,

    coalesce(ns.night_sleep_minutes, 0) as night_sleep_minutes,
    ns.night_sleep_segments,
    ns.longest_night_stretch_minutes,

    coalesce(n.nap_count, 0) as nap_count,
    coalesce(n.total_nap_minutes, 0) as total_nap_minutes,
    n.avg_nap_minutes,

    coalesce(f.feed_count, 0) as feed_count,
    coalesce(f.breast_feed_count, 0) as breast_feed_count,
    coalesce(f.bottle_feed_count, 0) as bottle_feed_count,
    f.total_volume_ml,
    fi.avg_feed_interval_minutes,

    w.avg_wake_window_minutes,
    w.max_wake_window_minutes,

    coalesce(np.diaper_count, 0) as diaper_count,
    coalesce(np.wet_diaper_count, 0) as wet_diaper_count,
    coalesce(np.dirty_diaper_count, 0) as dirty_diaper_count

from date_spine d
left join night_sleep ns on ns.baby_id = d.baby_id and ns.metric_date = d.metric_date
left join naps n on n.baby_id = d.baby_id and n.metric_date = d.metric_date
left join feeds f on f.baby_id = d.baby_id and f.metric_date = d.metric_date
left join feed_intervals fi on fi.baby_id = d.baby_id and fi.metric_date = d.metric_date
left join wake_windows w on w.baby_id = d.baby_id and w.metric_date = d.metric_date
left join nappies np on np.baby_id = d.baby_id and np.metric_date = d.metric_date
