{% macro make_temp_relation(base_relation, suffix='__dbt_tmp') %}
    
    {% set run_id = invocation_id | replace('-', '') %}
    {% set tmp_identifier = base_relation.identifier ~ '_' ~ run_id ~ suffix %}
    {% set tmp_relation = base_relation.incorporate(
                                path={"identifier": tmp_identifier}) -%}
                                
    {{ return(tmp_relation) }}

{% endmacro %}