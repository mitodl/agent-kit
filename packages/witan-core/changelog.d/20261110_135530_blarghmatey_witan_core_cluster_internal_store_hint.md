### Changed

- A store that stays unreachable at a Kubernetes Service name
  (`*.svc`, `*.svc.cluster.local`) from outside a pod says that the name
  resolves only inside its cluster and points at registering and selecting
  a `witan target add --remote-url --oidc-issuer` target, instead of reading
  like an outage.
