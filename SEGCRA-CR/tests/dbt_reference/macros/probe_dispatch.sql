{#- 探測用 macro:驗證 adapter.dispatch 依轉接器挑實作(dbt 先找 <轉接器>__,再退回 default__)。
    duckdb 與 sqlserver 各有實作,兩個目標的標準答案因此不同;沒有實作的轉接器會用 default__。 -#}
{% macro default__probe_fmt(value) %}CAST({{ value }} AS VARCHAR){% endmacro %}

{% macro duckdb__probe_fmt(value) %}CAST({{ value }} AS VARCHAR){% endmacro %}

{% macro sqlserver__probe_fmt(value) %}CONVERT(VARCHAR(30), {{ value }}){% endmacro %}
