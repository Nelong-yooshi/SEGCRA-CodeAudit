/*
Sample / synthetic example — 合成範例。
Created: 2026-05-22
Description:
Change Log:
- 2026-05-22 [REFACT] 定義出入帳通用篩選條件
*/


{# 【入帳/不排小額】純看代碼 #}
{% macro is_inward_all(model_name) %}
    {% set cfg = get_config()[model_name] %}

    (
        direction_flag = '0'
        AND op_code IN {{ cfg['inward_codes'] }}
    )
{% endmacro %}


{# 【入帳/排除小額】看代碼 + 吃 Config 裡的金額門檻 #}
{% macro is_inward_large(model_name) %}
    {% set cfg = get_config()[model_name] %}

    (
        direction_flag = '0'
        AND amount > {{ cfg['inward_threshold'] }}
        AND op_code IN {{ cfg['inward_codes'] }}
    )
{% endmacro %}

{# 【出帳/不排小額】純看代碼 #}
{% macro is_outward_all(model_name) %}
    {% set cfg = get_config()[model_name] %}

    (
        direction_flag = '1'
        AND op_code IN {{ cfg['outward_codes'] }}
    )
{% endmacro %}


{# 【出帳/排除小額】看代碼 + 吃 Config 裡的金額門檻 #}
{% macro is_outward_large(model_name) %}
    {% set cfg = get_config()[model_name] %}

    (
        direction_flag = '1'
        AND amount > {{ cfg['outward_threshold'] }}
        AND op_code IN {{ cfg['outward_codes'] }}
    )
{% endmacro %}

{# 【自助設備出帳】純看代碼 #}
{% macro is_kiosk_outward_all(model_name) %}
    {% set cfg = get_config()[model_name] %}

    (
        direction_flag = '1'
        AND op_code IN {{ cfg['kiosk_outward_codes'] }}
    )
{% endmacro %}
