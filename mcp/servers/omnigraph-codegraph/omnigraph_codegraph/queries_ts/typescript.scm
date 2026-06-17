; TypeScript / JavaScript symbol extraction.
; Shared by the typescript, tsx, and javascript parsers.

(function_declaration
  name: (identifier) @symbol.function)

(class_declaration
  name: (type_identifier) @symbol.class)

(method_definition
  name: (property_identifier) @symbol.method)

; const Foo = () => {...} / const Foo = function () {...}
(variable_declarator
  name: (identifier) @symbol.function
  value: [(arrow_function) (function_expression)])

(interface_declaration
  name: (type_identifier) @symbol.interface)

(type_alias_declaration
  name: (type_identifier) @symbol.type)

; extends Base — best-effort Inherits.
(class_declaration
  (class_heritage (extends_clause (identifier) @inherit.base)))

; imports — best-effort resolution.
(import_statement
  (import_clause (identifier) @import.name))

(import_statement
  (import_clause (named_imports (import_specifier name: (identifier) @import.name))))

; calls
(call_expression
  function: (identifier) @call.name)

(call_expression
  function: (member_expression property: (property_identifier) @call.name))
