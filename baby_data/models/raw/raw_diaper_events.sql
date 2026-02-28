with source as (

    select * from {{ source('baby_app', 'diaper_events') }}

),

renamed as (

    select
        id,
        baby_id,
        timestamp,
        has_urine,
        urine_volume,
        has_stool,
        stool_consistency,
        stool_color,
        diaper_type,
        notes,
        created_at,
        updated_at

    from source

)

select * from renamed
