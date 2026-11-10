### Security

- A token issued to the web UI's Keycloak client (`WITAN_UI_OIDC_CLIENT_ID`,
  read from the token's client id, which Keycloak sets as `azp`) can call only
  the UI's bound read tools (ADR 0011 §3), and `tools/list` shows it only
  those. It could previously call every tool, so a script injected into the
  deployed page could write to the shared graph as the person viewing it.
  Other clients keep every tool. A call to a tool that does not exist now
  fails as "not found or not authorized".
