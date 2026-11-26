select
    plan_id,
    billing_period,
    price
from {{ ref('plan_prices') }}
