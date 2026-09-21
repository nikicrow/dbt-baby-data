{#
  The assembled feature matrix: one row per prediction point, joining the three
  topic families onto the spine.

  This model owns only the CALENDAR and AGE columns, because unlike the three
  families they have no event source — they are pure functions of columns the
  spine already carries (prediction_time, age_days). Giving them their own
  table would add a join for no reuse.

  Everything else lives in a family, each independently usable:
    - ml_features_sleep    (fct_sleep_blocks)
    - ml_features_feeding  (stg_feeding_sessions)
    - ml_features_diaper   (stg_diaper_events)

  Adding a feature means editing ONE family. Adding a family means one new
  model and one more join here. A model about something other than sleep
  onset — feed timing, night wakings — can select the families it needs and
  ignore the rest.

  All three families select from ml_prediction_points and add columns only, so
  these joins cannot fan out or drop rows. assert_ml_tables_same_grain.sql
  proves that rather than trusting it.
#}

{{ config(materialized='table') }}

select
    p.prediction_id,
    p.baby_id,
    -- Grouping and splitting always; as a FEATURE it is an open experiment
    -- (plan §1.4) toggled in FeatureSpec, not decided here.
    p.baby_name,
    p.prediction_time,

    -- ===============================================================
    -- Calendar and time of day — owned here, see the header
    -- ===============================================================
    extract(hour from p.prediction_time)::int as hour_of_day,
    (extract(hour from p.prediction_time) * 60
        + extract(minute from p.prediction_time))::int as minutes_since_midnight,
    -- Cyclic encoding: time of day is a circle, and logistic regression can
    -- only draw straight lines. Without these, 23:50 and 00:10 sit 1,420 units
    -- apart instead of 20. Trees do not need them and will ignore them.
    sin(2 * pi() * (extract(hour from p.prediction_time) * 60
        + extract(minute from p.prediction_time)) / 1440) as tod_sin,
    cos(2 * pi() * (extract(hour from p.prediction_time) * 60
        + extract(minute from p.prediction_time)) / 1440) as tod_cos,
    (extract(hour from p.prediction_time) >= 19
        or extract(hour from p.prediction_time) < 7) as is_night_hours,
    extract(dow from p.prediction_time)::int as day_of_week,
    (extract(dow from p.prediction_time) in (0, 6)) as is_weekend,

    -- ===============================================================
    -- Age
    -- ===============================================================
    p.age_days,
    (p.age_days / 7)::int as age_weeks,
    round((p.age_days / 30.44)::numeric, 2) as age_months,

    -- ===============================================================
    -- The three topic families
    -- ===============================================================
    {{ dbt_utils.star(
        from=ref('ml_features_sleep'),
        relation_alias='s',
        except=['prediction_id', 'baby_id', 'prediction_time']
    ) }},
    {{ dbt_utils.star(
        from=ref('ml_features_feeding'),
        relation_alias='f',
        except=['prediction_id', 'baby_id', 'prediction_time']
    ) }},
    {{ dbt_utils.star(
        from=ref('ml_features_diaper'),
        relation_alias='d',
        except=['prediction_id', 'baby_id', 'prediction_time']
    ) }}

from {{ ref('ml_prediction_points') }} p
inner join {{ ref('ml_features_sleep') }}   s on s.prediction_id = p.prediction_id
inner join {{ ref('ml_features_feeding') }} f on f.prediction_id = p.prediction_id
inner join {{ ref('ml_features_diaper') }}  d on d.prediction_id = p.prediction_id
