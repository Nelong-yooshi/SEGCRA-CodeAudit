select id, amount
from {{ ref('txn_log') }}
where {{ is_large('amount') }}
