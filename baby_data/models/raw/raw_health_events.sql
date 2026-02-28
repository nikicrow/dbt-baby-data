with source as (

    select * from {{ source('baby_app', 'health_events') }}

),

renamed as (

    select
        id,
        baby_id,
        event_date,
        event_type,
        title,
        description,
        temperature_celsius,
        symptoms,
        treatment,
        healthcare_provider,
        follow_up_required,
        follow_up_date,
        attachments,
        notes,
        created_at,
        updated_at

    from source

)

select * from renamed