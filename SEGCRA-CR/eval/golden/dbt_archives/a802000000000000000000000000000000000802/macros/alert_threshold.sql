{% macro alert_threshold(rule_code) -%}
    {%- set thresholds = {
        'SHARED_A_EVAL': 50000,
        'SHARED_B_EVAL': 100000,
    } -%}
    {{- thresholds[rule_code] -}}
{%- endmacro %}
