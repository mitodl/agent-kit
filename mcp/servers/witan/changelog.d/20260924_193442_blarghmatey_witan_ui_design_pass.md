### Changed

- **The web UI has a typographic and colour system.** Alegreya Sans for the
  interface and Alegreya for the names of things (projects, tasks, memories),
  self-hosted because the page's CSP is `default-src 'self'`, with tabular
  figures wherever times and counts line up. Three state colours: blue for held
  work, yellow for a lapsed lease, red for a contradiction or cycle, each also
  carried by something other than hue. Board cards are edged by status, the
  detail panel's read state sits under its title rather than squeezed beside
  it, dense label columns drop their underlines until hovered, the Graph and
  Bridge canvases draw labels in the page's face, and the top bar no longer
  pushes the page sideways on a phone. The MCP Apps widgets share the
  stylesheet but not the fonts, which would have added about 190 KB of base64
  to each single-file widget, so they fall back to the system face.

### Added

- **A guide to the web UI**, `docs/web-ui.md` (mirrored to the docs site as
  "The witan web UI"), with screenshots of each tab taken from the Witan UI
  project's own tasks, sessions and memories.
