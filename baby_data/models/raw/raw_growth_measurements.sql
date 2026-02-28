with source as (

    select * from {{ source('baby_app', 'growth_measurements') }}

),

renamed as (

    select
        id,
        baby_id,
        measurement_date,
        weight_kg,
        length_cm,
        head_circumference_cm,
        measurement_context,
        measured_by,
        percentiles,
        notes,
        created_at,
        updated_at

    from source

)

select * from renamed