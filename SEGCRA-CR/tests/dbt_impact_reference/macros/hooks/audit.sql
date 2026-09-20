{# 對照用:被 model 的 post_hook 字串呼叫 #}
{% macro audit_hook() %}SELECT 1 AS audited{% endmacro %}
