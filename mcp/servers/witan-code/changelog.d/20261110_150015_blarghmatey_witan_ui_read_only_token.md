### Security

- The "no code graph yet, index it now?" offer is no longer made to a token-authenticated remote caller. It indexed the server's own checkout, so a remote caller (including the web UI, whose token is limited to read tools) could turn a read such as `code_interface_consumers` into a write by accepting the prompt. A local stdio server still makes the offer.
