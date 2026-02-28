with source as (

    select * from {{ source('baby_app', 'sleep_sessions') }}

),

renamed as (

    select
        id,
        baby_id,
        start_time,
        end_time,
        sleep_type,
        location,
        sleep_quality,
        sleep_environment,
        wake_reason,
        notes,
        created_at,
        updated_at

    from source

)

select * from renamed