-- The ML layer's central invariant: the spine, the labels, the three topic
-- feature families and the assembled feature table are all one row per
-- prediction point, with identical keys.
--
-- Fails loudly the moment a `where` clause quietly drops rows from one side, or
-- a lateral join fans out. Returns one row per mismatched key, naming the table
-- it disagrees with.

{% set ml_tables = [
    'ml_sleep_labels',
    'ml_features_sleep',
    'ml_features_feeding',
    'ml_features_diaper',
    'ml_sleep_features',
] %}

with points as (select prediction_id from {{ ref('ml_prediction_points') }})

{% for tbl in ml_tables %}
select prediction_id, {{ "'missing from " ~ tbl ~ "'" }} as problem
from points
where prediction_id not in (select prediction_id from {{ ref(tbl) }})

union all

select prediction_id, {{ "'in " ~ tbl ~ " but not in the spine'" }}
from {{ ref(tbl) }}
where prediction_id not in (select prediction_id from points)

{% if not loop.last %}union all{% endif %}
{% endfor %}
