{% macro safe_divide(numerator, denominator, default_value=0) %}
    CASE 
        WHEN {{ denominator }} = 0 OR {{ denominator }} IS NULL THEN {{ default_value }}
        ELSE CAST({{ numerator }} AS FLOAT) / CAST({{ denominator }} AS FLOAT)
    END
{% endmacro %}