{# 對照用:dbt 的 generic test 定義(test 區塊) #}
{% test not_negative(model, column_name) %}
    SELECT * FROM {{ model }} WHERE {{ column_name }} < 0
{% endtest %}
