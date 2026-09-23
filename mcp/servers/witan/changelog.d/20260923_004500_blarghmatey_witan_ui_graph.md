### Added

- **The UI's Graph tab: `witan graph`'s project and task graph, in the page.**
  It reads what the CLI reads (active projects and every task in scope, lifted
  off the unscoped 50-row cap) and draws the same nodes and edges, through a
  TypeScript port of `visualize.py`'s transform that a recorded fixture holds
  to the Python. Clicking a task opens the shared detail panel, and clicking a
  project opens its rollup. vis-network is bundled and loaded only when the tab
  is opened, rather than fetched from unpkg as the CLI's HTML file does, so the
  page reaches nothing but its own origin. The Closed filter toggles closed
  tasks without a re-read. `witan graph` keeps its terminal, HTML and DOT
  output.
