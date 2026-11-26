select
    account_id,
    segment,
    signup_date
from {{ ref('accounts') }}
