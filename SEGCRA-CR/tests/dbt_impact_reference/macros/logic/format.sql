{# 對照用:adapter.dispatch 的實作,依資料庫種類擇一 #}
{% macro default__fmt_amount(col) %}CAST({{ col }} AS VARCHAR(20)){% endmacro %}

{% macro sqlserver__fmt_amount(col) %}CONVERT(VARCHAR(20), {{ col }}){% endmacro %}
