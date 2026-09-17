{# 對照用:被 dbt_project.yml 的 on-run-end hook 呼叫 #}
{% macro log_run_end() %}SELECT 1 AS finished{% endmacro %}
