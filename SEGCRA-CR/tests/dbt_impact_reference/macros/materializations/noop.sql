{# 對照用:dbt 專屬的 materialization 語法(一般 Jinja 無法解析) #}
{% materialization noop, default %}
    {{ return({'relations': []}) }}
{% endmaterialization %}
