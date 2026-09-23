

{# 對照用:檔案開頭空行、空白控制符、區塊標記前後的空行、檔尾多個空行 #}
{%- set n = 2 -%}
SELECT {{ n }} AS n
{% if n > 1 %}
    , 'many' AS label
{% else %}
    , 'one' AS label
{% endif %}
FROM {{ ref('txn_log_net') }}



