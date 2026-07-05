{#
  Use a custom schema name as-is instead of dbt's default
  <target_schema>_<custom_schema> concatenation, so `+schema: marts`
  materializes into a schema literally named `marts` that the app
  backend can query without knowing the dbt target schema.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
