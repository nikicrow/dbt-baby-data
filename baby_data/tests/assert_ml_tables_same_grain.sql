-- The ML layer's central invariant: ml_prediction_points, ml_sleep_labels and
-- ml_sleep_features are one row per prediction point, with identical keys.
--
-- Fails loudly the moment a `where` clause quietly drops rows from one side, or
-- a lateral join fans out. Returns one row per mismatched key, with the side it
-- is missing from.

with points as (select prediction_id from {{ ref('ml_prediction_points') }}),
labels as (select prediction_id from {{ ref('ml_sleep_labels') }}),
features as (select prediction_id from {{ ref('ml_sleep_features') }})

select prediction_id, 'missing from ml_sleep_labels' as problem
from points where prediction_id not in (select prediction_id from labels)

union all
select prediction_id, 'missing from ml_sleep_features'
from points where prediction_id not in (select prediction_id from features)

union all
select prediction_id, 'in ml_sleep_labels but not in the spine'
from labels where prediction_id not in (select prediction_id from points)

union all
select prediction_id, 'in ml_sleep_features but not in the spine'
from features where prediction_id not in (select prediction_id from points)
