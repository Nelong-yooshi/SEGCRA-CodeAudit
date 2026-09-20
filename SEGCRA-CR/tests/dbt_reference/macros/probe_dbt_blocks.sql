{#- 探測用:真實 dbt 專案的 macro 目錄常見 dbt 專屬區塊(自訂 materialization、
    generic test)。Jinja 本身不認得這些標籤,少了處理,macro 目錄裡只要有一個
    檔案用到,整包 macro 就解析失敗、所有 model 都展不開。
    這個檔案同時放了一般 macro,用來驗證「區塊被略過,但同檔的 macro 仍可用」。 -#}
{% materialization probe_mat, default %}
  {%- set sql = "SELECT 1" -%}
  {{ return({'relations': []}) }}
{% endmaterialization %}

{% test probe_not_negative(model, column_name) %}
select * from {{ model }} where {{ column_name }} < 0
{% endtest %}

{% macro probe_block_neighbour(value) %}NEIGHBOUR({{ value }}){% endmacro %}
