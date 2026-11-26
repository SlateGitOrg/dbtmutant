with changes as (
    select
        account_id,
        event_id,
        event_date,
        event_type,
        plan_id,
        billing_period,
        lead(event_date) over (partition by account_id order by event_date, event_id) as next_change_date
    from {{ ref('stg_subscription_events') }}
    where event_type <> 'renewal'
),

periods as (
    select
        account_id,
        plan_id,
        billing_period,
        event_date as start_date,
        coalesce(next_change_date, cast(date '2025-12-31' + interval 1 day as date)) as end_date
    from changes
    where event_type <> 'cancel'
),

calendar as (
    select cast(day as date) as day
    from generate_series(date '2024-01-01', date '2025-12-31', interval 1 day) as t(day)
)

select
    p.account_id,
    c.day,
    p.plan_id,
    p.billing_period,
    case
        when p.billing_period = 'annual' then cast(round(pp.price / 12, 2) as decimal(12, 2))
        else pp.price
    end as list_mrr
from periods p
join calendar c
    on c.day >= p.start_date
    and c.day < p.end_date
join {{ ref('stg_plan_prices') }} pp
    on pp.plan_id = p.plan_id
    and pp.billing_period = p.billing_period
