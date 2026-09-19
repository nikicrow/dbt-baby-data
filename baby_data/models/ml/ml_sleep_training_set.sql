{#
  The 1:1 join of labels and features — what Python reads.

  A view, not a table: it is a pure join of two tables that are already
  materialised, so storing it again would double the disk for nothing.

  The join is inner on prediction_id. Both sides descend from
  ml_prediction_points and add columns only, so it cannot fan out or drop
  rows — assert_ml_tables_same_grain.sql proves that rather than trusting it.
#}

{{ config(materialized='view') }}

select
    l.prediction_id,
    l.baby_id,
    l.baby_name,
    l.prediction_time,
    l.calendar_date,

    -- Labels
    l.is_asleep_30_mins,
    l.is_asleep_60_mins,
    l.minutes_to_next_sleep,

    -- Splits
    l.split_forward_time,
    l.split_random_day,
    l.sample_bucket,

    -- Features
    {{ dbt_utils.star(
        from=ref('ml_sleep_features'),
        relation_alias='f',
        except=['prediction_id', 'baby_id', 'baby_name', 'prediction_time']
    ) }}

from {{ ref('ml_sleep_labels') }} l
inner join {{ ref('ml_sleep_features') }} f
    on f.prediction_id = l.prediction_id
