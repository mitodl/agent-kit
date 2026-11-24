### Added

- witan: Open Board (also a button on the Work view) opens the web UI's views (projects, board, waves, timeline, memory, graph and Bridge) in a panel for a workspace folder. The panel loads the bundle the installed witan ships and reads through the language server, so it holds no credential and makes no network request; its content security policy allows the bundle's own files and nothing else. It needs a witan whose language server has `witan.uiRead`, and says so when it does not.
