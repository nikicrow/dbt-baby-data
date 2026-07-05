with feeds as (

    select * from {{ ref('raw_feeding_sessions') }}

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
        f.id as feeding_id,
        f.baby_id,
        p.baby_name,
        p.date_of_birth,
        f.start_time,
        f.end_time,
        round(extract(epoch from (f.end_time - f.start_time)) / 60)::int as duration_minutes,
        f.feeding_type,
        f.breast_started,
        f.left_breast_duration,
        f.right_breast_duration,
        -- Sum of the per-side durations. For breast feeds this currently always
        -- equals duration_minutes (the tracker's total IS left + right), but it
        -- is derived independently of end_time so a divergence would be visible.
        case
            when f.left_breast_duration is not null or f.right_breast_duration is not null
                then coalesce(f.left_breast_duration, 0) + coalesce(f.right_breast_duration, 0)
        end as breast_duration_minutes,
        f.volume_offered_ml,
        f.volume_consumed_ml,
        f.start_time::date - p.date_of_birth as age_days,
        (f.start_time::date - p.date_of_birth) / 7 as age_weeks

    from feeds f
    inner join profiles p on p.baby_id = f.baby_id

)

select * from joined
