{#
  One row per sleep session with night/nap classification.

  is_night is computed here rather than reusing the transform script's
  sleep_type, so short night wakings count toward the night's total rather
  than being classed as naps:
    - starts 7pm-7am -> night, whatever the duration
    - starts 6-7pm   -> night only if longer than 3h (Ember's toddler
      bedtime was often before 7pm; short evening catnaps stay naps)
  A night is attributed to the date it started: a 2am segment belongs to
  the previous calendar date's night.
#}

with sleeps as (

    select * from {{ ref('stg_sleep_sessions') }}

),

classified as (

    select
        sleep_id,
        baby_id,
        baby_name,
        date_of_birth,
        start_time,
        end_time,
        duration_minutes,
        sleep_type,
        age_days,
        age_weeks,
        start_time::date as calendar_date,
        case
            when extract(hour from start_time) < 7 then true
            when extract(hour from start_time) >= 19 then true
            when extract(hour from start_time) >= 18 and duration_minutes > 180 then true
            else false
        end as is_night,
        case
            when extract(hour from start_time) < 7 then start_time::date - 1
            when extract(hour from start_time) >= 19 then start_time::date
            when extract(hour from start_time) >= 18 and duration_minutes > 180 then start_time::date
        end as night_date

    from sleeps

)

select * from classified
