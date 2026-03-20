# Infrastructure Skills

Conventions for infrastructure-as-code and secrets management.

| Skill | Description |
|-------|-------------|
| [`pulumi-modify-existing`](./pulumi-modify-existing/SKILL.md) | Modify existing stack entrypoint; never create new files; preserve `assumeRole` |
| [`vault-k8s-auth`](./vault-k8s-auth/SKILL.md) | Wire Vault K8s auth via `hvac` using env vars; never hardcode role or mount path |
