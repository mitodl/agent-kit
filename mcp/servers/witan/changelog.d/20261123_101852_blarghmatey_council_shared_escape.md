### Changed

- The language-server plugin escapes teammate-written text in hovers with witan-core's `escape_markdown` instead of its own copy, and `witan.lsp_plugin.plain` is gone. Hover output is unchanged. Requires witan-core 0.60.1.
