-- A prediction is only meaningful when the baby is awake — it is the premise of
-- the whole model. Any spine row falling inside a sleep block would be both
-- unanswerable and trivially labelled.

select
    p.prediction_id,
    p.baby_name,
    p.prediction_time,
    b.block_start,
    b.block_end

from {{ ref('ml_prediction_points') }} p
inner join {{ ref('fct_sleep_blocks') }} b
    on b.baby_id = p.baby_id
   and p.prediction_time >= b.block_start
   and p.prediction_time <  b.block_end
