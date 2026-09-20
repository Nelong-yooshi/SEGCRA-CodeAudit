{# 對照用:依名稱動態取用 macro —— 呼叫對象在執行期才知道 #}
{% macro call_by_name(macro_name, rule_name) %}
    {{ return(context[macro_name](rule_name)) }}
{% endmacro %}
