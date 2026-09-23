### Added

- **The web UI logs in on a deployed witan.** When `/ui/config.json` names an
  issuer, the page runs the authorization code flow with PKCE against it
  through oidc-spa, using the client id from `WITAN_UI_OIDC_CLIENT_ID`, and
  sends the access token as a bearer header on every `/mcp` call. Tokens are
  held in memory only; a reload goes back through Keycloak's SSO session
  without a prompt. A 401 restarts the login once (parallel 401s share it),
  and a 401 right after a fresh login stops for the life of the page with a
  message pointing at the Keycloak client's audience mapper, instead of
  redirecting forever. `witan ui` against a local
  store is unchanged: its config has no issuer, and the page sends no
  credential. The Keycloak `witan-ui` client ships in ol-infrastructure.
