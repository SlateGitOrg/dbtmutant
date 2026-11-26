with events as (
    select
        e.event_id,
        e.account_id,
        e.event_date,
        e.event_type,
        e.billing_period,
        pp.price,
        c.credit_rate
    from {{ ref('stg_subscription_events') }} e
    left join {{ ref('stg_plan_prices') }} pp
        on pp.plan_id = e.plan_id
        and pp.billing_period = e.billing_period
    join {{ ref('int_account_credits') }} c
        on c.account_id = e.account_id
    where e.event_type <> 'renewal'
),

priced as (
    select
        event_id,
        account_id,
        event_date,
        event_type,
        cast(
            round(
                case
                    when event_type = 'cancel' then cast(0 as decimal(12, 2))
                    when billing_period = 'annual' then cast(round(price / 12, 2) as decimal(12, 2))
                    else price
                end * (1 - credit_rate),
                2
            ) as decimal(12, 2)
        ) as mrr_after
    from events
),

movements as (
    select
        event_id,
        account_id,
        event_date,
        event_type,
        mrr_after,
        coalesce(
            lag(mrr_after) over (partition by account_id order by event_date, event_id),
            cast(0 as decimal(12, 2))
        ) as mrr_before
    from priced
)

select
    event_id,
    account_id,
    event_date as movement_date,
    case
        when event_type = 'start' then 'new'
        when event_type = 'reactivate' then 'reactivation'
        when event_type = 'cancel' then 'churn'
        when mrr_after > mrr_before then 'expansion'
        else 'contraction'
    end as movement_type,
    mrr_before,
    mrr_after,
    mrr_after - mrr_before as mrr_delta
from movements
where mrr_after <> mrr_before
