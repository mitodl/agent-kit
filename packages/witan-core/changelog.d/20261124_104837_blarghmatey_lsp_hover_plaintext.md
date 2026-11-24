### Fixed

- Language-server hovers read with backslashes outside VS Code. Neovim's hover
  float shows every markdown escape, and `escape_markdown` escaped every ASCII
  punctuation character, so a path read `lib\/maths\.py\:1`. It now escapes
  only a character that can start markup (a link or image bracket, a `(` right
  after `]`, `*`, `~`, a backtick, `|`, a `_` that is not inside a word, and
  a block marker at the start of the text), and writes `<` as `&lt;` and an
  entity's `&` as `&amp;`, so paths, URLs and `snake_case` names read as themselves. A bare
  URL that needs no escape is left for a GFM client to autolink, as a link to
  exactly its own text; one that does need an escape has its `://` or `www.`
  escaped too, so no autolink carries a backslash.
- A client that lists plain text first in `hover.contentFormat` (eglot without
  markdown-mode) now gets a plain-text hover, with the escapes, `**` and the
  link syntax of the markdown taken out, instead of markdown it shows raw.
