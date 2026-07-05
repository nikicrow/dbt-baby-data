{#
  Awake gaps between consecutive sleep sessions per baby.

  Daytime windows only: the window must open (previous sleep ends) between
  6am and 9pm. Gaps under 5 minutes (same sleep split across log entries)
  or over 6 hours (tracking gaps) are excluded as noise.
#}

with sleeps as (

    select
        baby_id,
        baby_name,
        date_of_birth,
        start_time,
        end_time
    from {{ ref('stg_sleep_sessions') }}

),

with_next as (

    select
        *,
        lead(start_time) over (partition by baby_id order by start_time, end_time)
            as next_sleep_start
    from sleeps

),

windows as (

    select
        baby_id,
        baby_name,
        end_time as wake_start,
        next_sleep_start as wake_end,
        round(extract(epoch from (next_sleep_start - end_time)) / 60)::int
            as wake_window_minutes,
        end_time::date as calendar_date,
        end_time::date - date_of_birth as age_days,
        (end_time::date - date_of_birth) / 7 as age_weeks

    from with_next
    where next_sleep_start is not null
      and next_sleep_start > end_time
      and extract(hour from end_time) between 6 and 20

)

select * from windows
where wake_window_minutes between 5 and 360
