-- mart_daily_metrics must have exactly one row per baby per date
select
    baby_id,
    metric_date,
    count(*) as row_count
from {{ ref('mart_daily_metrics') }}
group by baby_id, metric_date
having count(*) > 1
