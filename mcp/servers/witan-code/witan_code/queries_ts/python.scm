; Python symbol extraction.
; Capture names drive Symbol kind + edge construction in indexer.py.

(function_definition
  name: (identifier) @symbol.function)

(class_definition
  name: (identifier) @symbol.class)

; Base classes — used for best-effort Inherits edges.
(class_definition
  superclasses: (argument_list (identifier) @inherit.base))

; Imports — used for import-aware name resolution.
(import_statement
  name: (dotted_name (identifier) @import.name))

(import_from_statement
  name: (dotted_name (identifier) @import.name))

(import_from_statement
  name: (aliased_import (dotted_name (identifier) @import.name)))

; Calls — best-effort Calls/References edges.
(call
  function: (identifier) @call.name)

(call
  function: (attribute attribute: (identifier) @call.name))
