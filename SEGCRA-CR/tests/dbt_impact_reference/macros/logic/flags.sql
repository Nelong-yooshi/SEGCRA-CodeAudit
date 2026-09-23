{# 對照用:條件 macro,查註冊表取門檻 #}
{% macro flag_large(rule_name) %}
    {% set cfg = get_rules()[rule_name] %}
    ( amount > {{ cfg['large'] }} )
{% endmacro %}

{% macro flag_small(rule_name) %}
    {% set cfg = get_rules()[rule_name] %}
    CASE WHEN amount < {{ cfg['small'] }} THEN 1 ELSE 0 END
{% endmacro %}
