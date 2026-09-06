/*
Sample / synthetic example — 合成範例,代碼與門檻皆為虛構。
Created: 2026-05-22
Description: 各監控模型的代碼清單與門檻註冊表
Change Log:
- 2026-05-22 [REFACT] 建立 config 註冊表之初使架構
*/

{% macro get_config() %}

    {% set config = {
        'RETAIL_M1': {
            'inward_threshold': "1000",
            'outward_threshold': "1000",
            'inward_codes': "('OP01','OP02','OP03')",
            'kiosk_outward_codes': "('OP11','OP12')",
            'outward_codes': "('OP21','OP22','OP23')"
        },
    } %}

    {{ return(config) }}

{% endmacro %}
