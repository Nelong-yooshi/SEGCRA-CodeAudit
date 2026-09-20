-- 對照用:do、迴圈控制、macro 預設參數、有預設值的 var、execute
-- execute: {{ execute }}
{% set cols = [] %}
{% for c in ['account_id', 'amount', 'skip_me', 'op_code'] %}
    {% if c == 'skip_me' %}{% continue %}{% endif %}
    {% do cols.append(c) %}
{% endfor %}
SELECT {{ cols | join(', ') }}
    , {{ safe_divide('amount', 'balance_after') }} AS ratio
    , '{{ var("probe_unset_var", "fallback") }}' AS v
FROM {{ source('raw', 'T_SAMPLE_TXN') }}
