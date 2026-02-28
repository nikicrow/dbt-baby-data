with source as (
    select * from {{ source('baby_app', 'baby_profiles') }}
),

renamed as (
    select
        id,
        name,
        date_of_birth,
        birth_weight,
        birth_length,
        birth_head_circumference,
        gender,
        timezone,
        notes,
        created_at,
        updated_at,
        is_active
    from source
)

select * from renamed