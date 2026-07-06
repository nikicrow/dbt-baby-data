with sleeps as (

    select * from {{ ref('raw_sleep_sessions') }}

),

profiles as (

    select
        id as baby_id,
        name as baby_name,
        date_of_birth
    from {{ ref('raw_baby_profiles') }}

),

joined as (

    select
        s.id as sleep_id,
        s.baby_id,
        p.baby_name,
        p.date_of_birth,
        s.start_time,
        s.end_time,
        round(extract(epoch from (s.end_time - s.start_time)) / 60)::int as duration_minutes,
        s.sleep_type,
        s.start_time::date - p.date_of_birth as age_days,
        (s.start_time::date - p.date_of_birth) / 7 as age_weeks

    from sleeps s
    inner join profiles p on p.baby_id = s.baby_id
    where s.end_time > s.start_time

)

select * from joined
