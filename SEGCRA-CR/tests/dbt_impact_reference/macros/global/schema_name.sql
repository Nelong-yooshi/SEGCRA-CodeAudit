{# 對照用:覆寫 dbt 內建 macro —— dbt 自己會呼叫,專案裡找不到呼叫點 #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ target.schema }}
{%- endmacro %}
