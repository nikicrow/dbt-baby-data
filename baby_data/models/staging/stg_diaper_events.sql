with diapers as (

    select * from {{ ref('raw_diaper_events') }}

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
        d.id as diaper_id,
        d.baby_id,
        p.baby_name,
        p.date_of_birth,
        d.timestamp as event_time,
        d.has_urine,
        d.has_stool,
        d.timestamp::date - p.date_of_birth as age_days,
        (d.timestamp::date - p.date_of_birth) / 7 as age_weeks

    from diapers d
    inner join profiles p on p.baby_id = d.baby_id

)

select * from joined
