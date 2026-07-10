; SQL symbol extraction.
;
; dbt-style models are usually a bare `select` wrapped in Jinja ({{ }} / {% %})
; that this grammar can't parse (each templated span becomes an isolated
; ERROR node; the surrounding SQL still parses). Those files rarely contain a
; CREATE statement — the CTEs inside the query are the only reliably
; navigable structure, so they're captured alongside the CREATE-statement
; definitions more common in migrations/raw SQL.

(create_table
  (object_reference) @symbol.table)

(create_view
  (object_reference) @symbol.table)

(create_function
  (object_reference) @symbol.function)

(cte
  (identifier) @symbol.cte)
