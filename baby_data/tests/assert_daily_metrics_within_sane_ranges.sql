-- Guard against classification bugs producing impossible daily values
select *
from {{ ref('mart_daily_metrics') }}
where night_sleep_minutes < 0
   or night_sleep_minutes > 1080   -- 18h of night sleep in one night
   or total_nap_minutes < 0
   or total_nap_minutes > 720      -- 12h of naps in one day
   or nap_count > 15
   or feed_count > 30
   or diaper_count > 25
   or age_days < 0
