### Changed

- A store that stays unreachable at a Kubernetes Service name
  (`*.svc`, `*.svc.cluster.local`) from outside a pod says that the name
  resolves only inside its cluster and points at
  `witan target add <name> --remote-url`, instead of reading like an outage.
