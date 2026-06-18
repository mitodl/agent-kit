; TypeScript / JavaScript / JSX / TSX symbol extraction. All JS and TS variants
; are parsed with the tsx grammar (a superset), so this single query covers them.

(function_declaration
  name: (identifier) @symbol.function)

(generator_function_declaration
  name: (identifier) @symbol.function)

(class_declaration
  name: (type_identifier) @symbol.class)

(method_definition
  name: (property_identifier) @symbol.method)

; const Foo = () => {...} / const Foo = function () {...}
(variable_declarator
  name: (identifier) @symbol.function
  value: [(arrow_function) (function_expression)])

; class fields holding arrows: render = () => {...}
(public_field_definition
  name: (property_identifier) @symbol.method
  value: (arrow_function))

(interface_declaration
  name: (type_identifier) @symbol.interface)

(type_alias_declaration
  name: (type_identifier) @symbol.type)

(enum_declaration
  name: (identifier) @symbol.enum)

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
