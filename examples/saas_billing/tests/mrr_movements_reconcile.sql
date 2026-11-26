-- The month-over-month change in closing MRR (from the daily fact, built off the calendar) must
-- equal the sum of that month's MRR movements (built off the raw events). Returns the months
-- where the two independent derivations disagree.
with movement_totals as (
    select
        date_trunc('month', movement_date) as month_start,
        sum(mrr_delta) as moved
    from {{ ref('fct_mrr_movements') }}
    group by 1
),

closing as (
    select
        day as month_end,
        sum(mrr) as closing_mrr
    from {{ ref('fct_mrr_daily') }}
    where day = last_day(day)
    group by 1
),

changes as (
    select
        month_end,
        closing_mrr - coalesce(lag(closing_mrr) over (order by month_end), 0) as change
    from closing
)

select
    c.month_end,
    c.change,
    m.moved
from changes c
left join movement_totals m
    on date_trunc('month', c.month_end) = m.month_start
where c.change <> coalesce(m.moved, 0)
