with bounds as (
    select max(day) as last_day_of_data
    from {{ ref('fct_mrr_daily') }}
),

month_end_rows as (
    select
        f.segment,
        f.account_id,
        f.day as month_end,
        f.mrr
    from {{ ref('fct_mrr_daily') }} f
    cross join bounds b
    where f.day = last_day(f.day)
        and f.day <= b.last_day_of_data
)

select
    segment || '-' || cast(month_end as varchar) as segment_month_key,
    segment,
    month_end,
    count(distinct account_id) as active_accounts,
    sum(mrr) as mrr,
    sum(mrr) * 12 as arr
from month_end_rows
group by segment, month_end
