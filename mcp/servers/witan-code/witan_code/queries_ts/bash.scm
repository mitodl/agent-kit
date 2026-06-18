; Bash / Zsh symbol extraction. The tree-sitter bash grammar covers most zsh.

; function foo() { ... }  and  foo() { ... }
(function_definition
  name: (word) @symbol.function)

; calls — best-effort Calls/References edges to other functions.
(command
  name: (command_name (word) @call.name))
