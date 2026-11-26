select
    event_id,
    account_id,
    event_date,
    event_type,
    plan_id,
    billing_period
from {{ ref('subscription_events') }}
