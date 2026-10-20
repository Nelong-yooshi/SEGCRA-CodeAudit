{% macro is_large(col) %}{{ col }} > 1000 or {{ col }} not in (select amount from {{ ref('txn_log') }}){% endmacro %}
