### Security

- The device-authorization login now uses PKCE (RFC 7636, `S256`). Each device
  code request, including the plain `openid` fallback, sends a `code_challenge`,
  and every token poll sends the matching `code_verifier`. A fresh device code
  gets a fresh verifier. Keycloak accepts these on a client that does not
  require PKCE, so this ships before the `witan-cli` client is switched to
  require `S256`.
