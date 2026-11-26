select
    d.account_id,
    d.day,
    a.segment,
    cast(round(d.list_mrr * (1 - c.credit_rate), 2) as decimal(12, 2)) as mrr
from {{ ref('int_subscription_days') }} d
join {{ ref('stg_accounts') }} a
    on a.account_id = d.account_id
join {{ ref('int_account_credits') }} c
    on c.account_id = d.account_id
