
with source as (

    select * from {{ source('baby_app', 'feeding_sessions') }}

),

renamed as (

    select
        id,
        baby_id,
        start_time,
        end_time,
        feeding_type,
        breast_started,
        left_breast_duration,
        right_breast_duration,
        volume_offered_ml,
        volume_consumed_ml,
        formula_type,
        food_items,
        appetite,
        notes,
        created_at,
        updated_at

    from source

)

select * from renamed