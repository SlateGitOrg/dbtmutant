select
    credit_note_id,
    invoice_id,
    credit_amount,
    issued_date
from {{ ref('credit_notes') }}
