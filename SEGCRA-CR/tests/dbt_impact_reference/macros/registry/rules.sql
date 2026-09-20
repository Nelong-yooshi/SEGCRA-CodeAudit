{# 對照用:規則註冊表(比照範例的 config.sql),回傳 dict #}
{% macro get_rules() %}
    {% set rules = {
        'ALPHA': {'large': 1000, 'small': 10},
        'GAMMA': {'large': 5000, 'small': 50},
        'DELTA': {'large': 2000, 'small': 20},
        'ZETA':  {'large': 3000, 'small': 30},
    } %}
    {{ return(rules) }}
{% endmacro %}
