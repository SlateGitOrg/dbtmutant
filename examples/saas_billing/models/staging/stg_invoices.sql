select
    invoice_id,
    account_id,
    invoice_date,
    gross_amount
from {{ ref('invoices') }}
where gross_amount > 0
