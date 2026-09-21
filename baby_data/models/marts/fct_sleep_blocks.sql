{#
  One row per CONTINUOUS asleep interval per baby.

  fct_sleep_sessions contains records that overlap — the same sleep logged
  across two entries (0.5% of Ember's rows, 0.2% of Imogen's). Left alone, the
  seam between two overlapping records reads as a moment of wakefulness that
  never happened. This model collapses any chain of sessions where each starts
  at or before the running maximum end_time of the ones before it into a single
  block.

  Standard gaps-and-islands: flag each row that opens a new island, cumulative-
  sum the flags into an island id, group by it.

  Block-level attributes (is_night, night_date, age) are taken from the FIRST
  session in the block, so a night that starts at 19:30 and absorbs a 23:50
  record is still attributed to the date it started.

  Deliberately general, not ML-specific: the ML prediction spine is one
  consumer, but "when was she actually asleep, continuously" is the right way
  to ask any longest-stretch question.
#}

with sessions as (

    select * from {{ ref('fct_sleep_sessions') }}

),

flagged as (

    select
        *,
        -- 1 when this session starts after every earlier session has ended,
        -- i.e. it opens a new block. The running max (rather than the previous
        -- row's end_time) handles a short session wholly contained in a longer
        -- one, which would otherwise look like it opened a block.
        case
            when start_time <= max(end_time) over (
                partition by baby_id
                order by start_time, end_time
                rows between unbounded preceding and 1 preceding
            ) then 0
            else 1
        end as is_new_block

    from sessions

),

islands as (

    select
        *,
        sum(is_new_block) over (
            partition by baby_id
            order by start_time, end_time
            rows between unbounded preceding and current row
        ) as block_index

    from flagged

),

with_block_attributes as (

    select
        *,
        first_value(is_night)    over w as block_is_night,
        first_value(night_date)  over w as block_night_date,
        first_value(age_days)    over w as block_age_days,
        first_value(age_weeks)   over w as block_age_weeks

    from islands
    window w as (
        partition by baby_id, block_index
        order by start_time, end_time
    )

),

blocks as (

    select
        baby_id,
        baby_name,
        block_index,
        block_is_night as is_night,
        block_night_date as night_date,
        block_age_days as age_days,
        block_age_weeks as age_weeks,
        min(start_time) as block_start,
        max(end_time) as block_end,
        count(*)::int as session_count

    from with_block_attributes
    group by 1, 2, 3, 4, 5, 6, 7

)

select
    {{ dbt_utils.generate_surrogate_key(['baby_id', 'block_start']) }} as sleep_block_id,
    baby_id,
    baby_name,
    block_start,
    block_end,
    round(extract(epoch from (block_end - block_start)) / 60)::int as block_duration_minutes,
    session_count,
    is_night,
    night_date,
    block_start::date as calendar_date,
    age_days,
    age_weeks

from blocks
