select
    a.account_id,
    cast(
        coalesce(sum(cn.credit_amount) / nullif(sum(i.gross_amount), 0), 0) as decimal(7, 4)
    ) as credit_rate
from {{ ref('stg_accounts') }} a
left join {{ ref('stg_invoices') }} i
    on i.account_id = a.account_id
left join {{ ref('stg_credit_notes') }} cn
    on cn.invoice_id = i.invoice_id
group by a.account_id
