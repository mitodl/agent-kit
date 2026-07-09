; HCL symbol extraction (Terraform / Packer / Vault-policy style configs).
;
; Each labeled block (resource/variable/output/module/data/source/path, …)
; becomes a Symbol keyed on its LAST label — the part that's actually unique
; (`resource "aws_instance" "foo"` -> foo, not the shared "aws_instance" type;
; `variable "app_name"` -> app_name; Vault's `path "secret/data/*"` -> the
; path itself). The `.` anchor requires the label be immediately followed by
; the opening brace, which is only true of the last label when there are two.
;
; Unlabeled blocks (locals, bare provider blocks) aren't captured — there's
; no unique text to key them on.

(block
  (string_lit) @symbol.block
  .
  (block_start))
