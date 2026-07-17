{#
  Use a custom schema name as-is instead of dbt's default
  <target_schema>_<custom_schema> concatenation, so `+schema: marts`
  materializes into a schema literally named `marts` that the app
  backend can query without knowing the dbt target schema.

  The `ci` target is the exception: it keeps dbt's default prefixing so a
  PR build lands in <target_schema>_marts (e.g. ci_pr_42_marts) instead of
  overwriting the real `marts` tables the app reads.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- elif target.name == 'ci' -%}
        {{ target.schema }}_{{ custom_schema_name | trim }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
