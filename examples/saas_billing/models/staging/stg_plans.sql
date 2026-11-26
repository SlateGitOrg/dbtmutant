select
    plan_id,
    plan_name
from {{ ref('plans') }}
