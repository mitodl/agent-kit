# witan-infra: a map of running infrastructure as witan's third layer (spec)

Status: proposed (spec phase). Three decisions are left to the owner; they are
listed in [Open decisions](#16-open-decisions), each with a recommendation.
Project: `wp-witan-infra-digital-twin-of-running-infrastructu-544088`
Epic: task `tk-epic-witan-infra-digital-twin-of-running-infrast-3ccc57`
Repos: agent-kit (the package), ol-infrastructure (the first deployment)
Basis: the gating spikes run on 2026-11-19 (attribution hit rate, secret
handling in the state export, omnigraph engine limits, graph size and read
latency, environment derivation, Cedar bundle scope), and
[ADR 0013](../../explanation/decisions/0013-one-infra-graph-with-a-fixed-id.md)
for the graph topology.

Source anchors are against agent-kit `origin/main` @ `5287dc96` and
ol-infrastructure `origin/main` @ `81eb074db`. Engine claims are against
omnigraph 0.15.0 (`373834b4`). Measurements against the Pulumi state backend
were taken 2026-11-19 over all 330 stacks.

## 1. Goal

witan's memory layer (`council`) and code layer (`code-*` graphs and the
`code-bridge`) both stop at the repository boundary. An agent can ask who calls
a function and which repo provides an env var, but not whether that code is
deployed, what talks to it in production, which datastore it reads, or what
production surface moves if the function changes.

`witan-infra` (directory `mcp/servers/witan-infra/`, import `witan_infra`,
dist `witan-infra`, CLI `witan infra …`, MCP tools `infra_*`) ingests
declarative infrastructure (IaC state, Kubernetes objects) and observed runtime
signals (trace topology, metrics, cloud inventory) into one graph of
infrastructure components, the zones they run in, and the services they
compose, and joins it to the code and memory layers.

Three uses drive every decision below:

- Blast radius: a code change mapped to the production services, routes and
  datastores it can move.
- Situational awareness for agents: what runs where, in which environment,
  declared by which IaC file, bound to which config keys.
- Q&A about the estate, with the coverage of each answer stated alongside it.

Non-goals: alerting, health dashboards, cost reporting, and any write to
infrastructure. The observability MCP servers already answer health questions;
witan-infra links to them rather than competing with them.

## 2. Organization-agnostic by construction

The design has to adapt to other organizations' naming and infrastructure
conventions. MITODL's stack (Pulumi on an S3 backend, EKS, Grafana Cloud,
APISIX, the `ol.mit.edu/*` label taxonomy) is the first instance and the
feasibility evidence, never the schema.

The rule: org-specific knowledge lives only in configuration and in collectors.
Node types, edge types, tool contracts and join-key code carry none of it.
Concretely:

- Attribute mapping ships OpenTelemetry semantic conventions as defaults. An
  org's keys are prepended to the candidate list for a slot, never substituted,
  so an org adds vocabulary without losing the standard fallback.
- Environment derivation, the service set, project-to-service maps and synonym
  tables are config (see [Environment derivation](#7-environment-derivation-is-a-config-rule)
  and [Identity and attribution](#6-identity-and-attribution)).
- Collectors register through an entry-point group, including the built-in
  ones, so an org's own collector takes the same path ours do
  ([Collectors](#9-collectors)).

A reviewer should reject any change that puts an org's label key, stack naming
pattern or service name in Python outside a collector or a shipped example
config.

## 3. Read IaC state, not IaC source

The declarative side comes from IaC state exports, not from parsing IaC
programs. Each resource record in a Pulumi export carries:

- `urn` (declared identity) and provider-qualified `type`
- `id` and identifiers in `outputs` (e.g. `arn`): the cloud-native identity
- `parent`, `dependencies`, `propertyDependencies`: the declared dependency
  graph, exact and free
- `sourcePosition`, e.g. `project:///data_tier.py#483`, a file and line in the
  IaC repo relative to the directory holding `Pulumi.yaml`
- `created`, `modified`, and tags or labels in `outputs`

Measured 2026-11-19 against `s3://mitol-pulumi-state`: 330 stacks, 23,229
resource records of 402 distinct types, 476 MB in total, largest stack 15.5 MB,
median 0.33 MB. A plain `pulumi stack export` of all 330 at 8-way parallelism
took 3 min 3 s with no failures. 22,291 records carry a `project://` relative
`sourcePosition`, which resolves to a file in the IaC repo and therefore to a
witan-code `CodeFile` slug. That join is exact, not heuristic, wherever the
project's source is in the IaC repo; 12 projects in state have no source
directory any more (legacy or orphaned stacks), and their resources are counted
in coverage as unresolvable rather than joined.

Consequences:

- The collector abstraction is "IaC state export to resource records". Pulumi is
  the first implementation. Terraform (`terraform show -json`) and
  CloudFormation stack-resource listings have the same shape; that parity is
  asserted, not yet proven, and the Terraform parity spike gates the IaC
  collector phase on it.
- `iac_line` is a per-collector capability (Pulumi has it, Terraform does not),
  not a schema assumption.
- A cloud-provider collector is needed only for resources no IaC declares. That
  delta ("what is running that nobody declared?") becomes a first-class output
  in [Phase 8](#17-task-dag-ratified-with-changes) instead of the backbone.
- The regex and YAML scraping of Pulumi programs in witan-code's bridge
  (`_service_anchors` at `witan_code/bridge_extractors.py:580`,
  `_pulumi_env_vars` at `:554`) is retired once the infra graph serves the same
  facts, not extended.

## 4. Coverage is partial, and the tool contract says so

The declarative side is far more complete than the observed side. In the week
2026-11-12 to 2026-11-19, Tempo carried 48 production `service.name` values,
against 57 services in the configured service set and 330 IaC stacks. Traces
also skew to what is instrumented, so an absent observed edge is not evidence of
an absent dependency.

So every infra read returns a `coverage` block (what sources contributed, over
what scopes, how fresh, and what is known to be missing), and no infra read
returns a bare `None` or a bare list. "Nothing depends on X" without coverage is
exactly the confident wrong answer this layer exists to prevent. Coverage is
part of the result shape from the first tool, not a diagnostic added later.

## 5. Graph and schema

### 5.1 One graph with a fixed id, `infra`

All infra data lives in one omnigraph graph whose id is the constant
`INFRA_GRAPH_ID = "infra"` in `witan_core.graph_ids` (beside `BRIDGE_GRAPH_ID`
and `code_graph_id`, which ol-infrastructure already imports from there rather
than mirroring). Not one graph per environment, cloud or cluster: the
cross-environment questions are the valuable ones, and omnigraph has no
cross-graph traversal. The reasoning and the alternatives are in
[ADR 0013](../../explanation/decisions/0013-one-infra-graph-with-a-fixed-id.md),
including the sizing that backs it ([5.6](#56-size-and-read-cost)).

There are no branch views. Production has no private working copy, and "what
would this PR change" is a `pulumi preview` question, not a graph read.

### 5.2 Value spaces are `String @index`, closed vocabularies are validated in Python

No `enum` in the infra schema. Provider, vendor and org value spaces (resource
`type`, label values, provider names) are open. The closed vocabularies
(`role`, `presence`, `completeness`, `attributed_by`, `none_reason`) are stored
as `String @index` and validated as `Literal` types in the pydantic models, so
adding a role is a package release, not a cluster schema change (which costs an
image rebuild, a `cluster apply` and a server restart). This also takes the
schema-evolution spike off the critical path: there is no enum variant to add.

`role` is the field queries filter on (`workload`, `route`, `datastore`,
`queue`, `cache`, `bucket`, `network`, `identity`, `secret_store`, `dns`,
`certificate`, `config`, `other`). `type` is the raw provider type string,
kept as data. The provider-type-to-role table is shipped config with an org
override layer.

### 5.3 Node types

| Node | Key | What it is |
|---|---|---|
| `Zone` | `slug` | One recursive type for account, region, network, cluster, namespace and tenant, with `kind` and a `parent` edge. `mitx` in stack `mitx.QA` is a Zone, not part of the environment. |
| `Resource` | `slug` = canonical id | One infrastructure object at the resource grain (an RDS instance, a Kubernetes Deployment, an ApisixRoute, a Route53 record). |
| `Service` | `slug` | The runtime grain: a thing a team names, deploys and an OTel `service.name` reports. Environment-independent; per-environment presence is carried by its resources. |
| `InfraBinding` | `slug` | A configuration key bound to a workload: `key` (the name, in the same normalised vocabulary as the bridge's `env_var` contract keys), `value_present`, `value_ref` (e.g. `vault:<mount>/<path>`, `k8s-secret:<ns>/<name>#<key>`). Never a value. |
| `Alias` | `slug` | An alternative identifier for a node, with its scheme and source. Semantic aliasing is data here, never a string rule. |
| `UnresolvedIdentity` | `slug` | Something a collector saw and could not map. Makes identity coverage measurable. |
| `ExternalDependency` | `slug` | An observed peer that matches nothing declared. Visible as unreconciled, never guessed into an existing node. |
| `Sighting` | `slug` | What one collector saw of one object in one scope ([10.2](#102-per-source-sightings-projected-objects)). The only rows a collector's output becomes directly. |
| `SyncRun` | `slug` | One collector run over one scope: `collector`, `scope`, `status` (`running`, `complete`, `failed`), `started_at`, `finished_at`, `watermark`, counts. Written `running` first and set `complete` last. |
| `SyncLease` | `slug` | The single-run lease ([10.1](#101-one-writer-one-run-at-a-time)). |

Common properties on `Resource` (and, where they apply, on the others):
`environment`, `presence` (`declared`, `observed`, `both`), `first_seen`,
`last_seen`, `missing_since`, `iac_repo`, `iac_path`, `iac_line`,
`native_id`, `type`, `role`, `name`, `service` (denormalised attribution, for
single-condition filters and the BFS fallback), and `projected_by` (the run
that last wrote the row). Which collector saw an object, in which scope and
run, lives on its `Sighting` rows, not on the object.

Declared and observed facts about the same object reconcile onto one node with
the `presence` discriminator. Reads filter with match shorthand, so drift
(`presence = "declared"` with no observation, or the reverse) has to be a field
value, not a join.

### 5.4 Edge types: provenance in the type, bookkeeping in properties

Different relations get different edge types, because "IaC declares A depends
on B" and "traces show A calls B" are different claims that fail differently:

| Edge | From -> To | Source |
|---|---|---|
| `InZone` | Resource/Zone -> Zone | declared |
| `DeclaredDependsOn` | Resource -> Resource | IaC `parent`/`dependencies` |
| `StackDependsOn` | Zone -> Zone | IaC `StackReference` (the org's own statement of inter-system topology) |
| `ComposedOf` | Service -> Resource | computed attribution ([6](#6-identity-and-attribution)) |
| `BindsConfig` | Resource -> InfraBinding | declared workload env and secret refs |
| `RoutesTo` | Resource -> Resource | declared ingress/route to backend |
| `ObsCallsService` | Service -> Service | observed (trace service graph) |
| `ObsUsesResource` | Service -> Resource | observed (span attributes: `db.*`, `messaging.*`, `server.address`) |
| `AliasOf` | Alias -> any | identity |
| `UndeclaredIn` | Resource -> Zone | cloud inventory delta |

The August scoping assumed omnigraph edges were property-free. They are not in
0.15 (the council schema already puts `confidence` and `role` on keyed edges),
so every infra edge type is declared `{ @key(@src, @dst) ... }` and carries
`edge_key` (`<src>|<dst>`, `@index`, so the reconciler can delete edges by key),
`projected_by`, and, where it applies, `attributed_by`, `confidence` and
`first_seen`/`last_seen`. Keying is required, not stylistic: merging an
unkeyed edge twice stores two rows, and an unbound traversal hides the
duplicate.

Edges are safe here where they were not in the bridge because witan-infra has
exactly one writer (the scheduled reconciler), not many concurrent indexers.

### 5.5 What is deliberately not modelled

- No numeric measurement field anywhere in the schema (rates, latencies, error
  counts, costs). The existence of an observed relation is batched; its
  magnitude is fetched live ([11](#11-reads-and-batch-existence-with-live-magnitude)).
- No configuration values, secret or not ([8](#8-secret-handling-allowlist-never-denylist)).
- No pods, ReplicaSets or other controller-owned churn. The workload object is
  the grain.
- No per-revision or per-image-tag history. `modified` and `last_seen` are the
  only time fields.
- No GitHub, Sentry or incident-catalog objects as infrastructure. They
  describe repos and processes, not running systems; where they help
  attribution they feed the `manual` map as data.
- No Helm-rendered or `ConfigGroup`-applied children from IaC state: Pulumi
  state does not hold them. The Kubernetes collector supplies those.

### 5.6 Size and read cost

Counted from all 330 exports (23,229 records), not projected from a sample:

| Type | Count | Basis |
|---|---|---|
| `Resource` | 20,577 | measured: 16,909 cloud objects and 3,668 component resources; 316 stack roots, 1,162 providers and 1,174 `StackReference`s excluded |
| `DeclaredDependsOn` | about 37.3k | 24,201 dependency and 10,197 parent edges measured (parent links to the stack root dropped), up to 2,901 more from cross-stack references |
| `InfraBinding` | about 14k | 13.8k measured from declared inputs (12,325 container env vars, Vault paths, ConfigMap and Secret keys); up to about 20k once Helm values and `envFrom` keys count |
| `Zone` | about 250 | 144 environment and namespace pairs measured |
| `Alias` | about 1.5k | 364 hostnames measured |
| `Service` | 57 | the configured service set ([6.3](#63-the-service-set-is-config)) |
| Kubernetes-observed additions | 2k to 10k | 693 workload and routing objects plus 1,577 ConfigMaps and Secrets measured in the four clusters reachable read-only (QA and CI), scaled by estimate |

Total: about 36k object nodes and on the order of 100k edges (the edge figure
is an estimate across all edge types), inside the 20k to 100k node range the
topology decision assumed. The write model in [10.2](#102-per-source-sightings-projected-objects)
adds one `Sighting` per collector per object, about 21k with the IaC collector
alone.

Local read cost on omnigraph 0.15 (median of 5): all `Resource` rows with 10
columns in 0.24 s at 1× (20,577 rows) and 1.71 s at 3× (61,731 rows); all
`DeclaredDependsOn` edges in 0.21 s at 1× (37,299) and 0.66 s at 3× (111,897).
Against the deployed server, a read-only 4,100-row bulk read of the
`code-bridge` graph took 2.70 s end to end, a 0-row read 2.54 s, and a call
reading no graph 1.27 s: at this size the cost is round trips, not rows. A
deployed read at 20k rows was not measured, because none exists read-only and
creating one would be a write.

`limit 1000000` returned exactly the loaded row count at both scales. A `limit`
below the row count truncates silently (the reported `row_count` equals the
limit, with no flag), so the reconciler and any reader that needs every row
checks its result against a `count()` read and fails loudly on a mismatch.

## 6. Identity and attribution

### 6.1 The granularity mismatch

The right key for a resource is its provider-native id. The right key for a
service is its runtime name. Many resources compose one service, and some
resources (a shared VPC, a data-lake bucket, an EKS cluster) compose none.
Forcing one grain onto the other loses information either way, so both grains
are modelled and joined by `ComposedOf`, a computed and auditable projection:
each edge records `attributed_by`, the rung of the ladder below that produced
it. A shared datastore is `ComposedOf` zero services, with a reason, rather than
being forced under one.

### 6.2 The attribution ladder

Measured 2026-11-19 over the production-representative set (113 stacks, 5,600
physical resources, GitHub/Sentry/Rootly administration excluded). The first
ladder as scoped attributed 61.8% to a service, put 34.3% correctly at `none`
(shared or zone-scoped) and missed 3.9% (217 resources). Excluding the correct
`none`s, the hit rate was 94.1% (95.5% across all stacks), above the 80% gate.
Tags and labels alone reach only 38.4% of resources in application stacks: the
rung carrying the result is `stack`, which is a project-to-service map in all
but name.

The ladder, in order, first satisfiable rung wins:

1. `manual`: the org's config. A project-to-service map (which is what made
   `stack` work, now declared explicitly), per-URN overrides, and projects
   declared multi-service so the stack rung declines for them (MITODL's edxapp
   project deploys several edxapp installations and forum).
2. `tag`: a configured tag key (MITODL: `Application`), through the synonym
   table.
3. `label`: configured Kubernetes label keys (OTel/`app.kubernetes.io/part-of`
   by default; MITODL prepends `ol.mit.edu/application`). Not
   `ol.mit.edu/service`, which is product-scoped and would label learn-ai as
   mit-learn.
4. `stack`: the owning IaC unit maps to exactly one service in the manual map.
5. `parent`: inherit from an attributed ancestor, or (unmeasured variant) a
   component inherits its children's single service. As first specified it
   never fired, because tags sit on leaves.
6. `alias_identity`: shares a native identifier with an attributed resource.
   All 19 inspected production hits were correct.
7. `image_repo`: a registry or pull-through-cache prefix matches the image of an
   attributed workload (50 production misses were ECR lifecycle policies).
8. `name_token`: a delimited service token in the URN name or physical id,
   gated to shared stacks and excluding configured deployment and product
   tokens. About 190 of the 210 single-token misses look correct by name; that
   estimate was not checked with each stack's owner.
9. `none`, with `none_reason`: `shared` (two or more services consume it),
   `zone_scoped` (per-tenant or per-network, attached by `InZone` instead),
   `no_signal`, `out_of_scope`.

Dropped from the scoped ladder: `alias_consumer` ("referenced by exactly one
service's IaC inputs"). On inspection 23 of its 83 production hits were right;
the rest were shared subnets, StorageClasses and buckets that happen to have
one IaC consumer. The consumer count stays as evidence: two or more consumers
is what sets `none_reason = shared`.

Estimated result with these changes: about 98.5% of per-service production
resources attributed. That figure combines measured rungs with the unverified
precision estimate for `name_token`; Phase 2 re-runs it as a test over the real
exports before Phase 2 merges.

### 6.3 The service set is config

What counts as a service is an org decision, declared in config with its
synonyms. MITODL's first instance has 57 entries (each `ol-application-*`
project with state, plus release-bot, forum and devlake) and a synonym table of
18 entries: 16 the spike measured with, plus `learn-webapp` and
`learn-nextjs`, which its Tempo check showed missing (e.g.
`openedx-platform`, `lms-edxapp` -> edxapp; `ovs` ->
odl-video-service; `learn-webapp` -> mit-learn). The synonyms carry real weight:
930 of 2,264 label hits resolve only through them. With the two added
synonyms, 46 of the 48 production Tempo service names seen 2026-11-12 to
2026-11-19 map onto the set; the other 2 (`traefik`, `apisix`) are platform
components and correctly map to none. Confirming this set is one of the [open decisions](#16-open-decisions).

### 6.4 Canonical ids are deterministic

A configured, ordered list of identity schemes per node type; the first
satisfiable scheme gives the canonical id, every other scheme becomes an
`Alias` row, and genuine collisions are recorded as aliases of each other.
Union-find is rejected: it is order-dependent, so a canonical id could flip
between runs with collection order and invalidate every edge pointing at it,
which in a merge-by-slug store is both catastrophic and near-invisible.

Component ids, used everywhere (tool input and output alike):
`<kind>:<name>[@<environment>]`, e.g. `service:mit-learn`,
`datastore:mitlearn-db@production`.

### 6.5 The normaliser is mechanical

Lowercase, collapse `[^a-z0-9]+` to `-`, trim. Nothing else: no suffix
stripping. `mitxonline-webapp` -> `mitxonline` would work by suffix rule, but
`learn-webapp` -> `learn` does not (the service is `mit-learn`, a peer calls it
`mitopen`, and `learn-nextjs` is a different service), and
`keycloak-production` -> `keycloak` collides across environments. Semantic
equivalence is an `Alias` row with its evidence.

## 7. Environment derivation is a config rule

`environment` is on every node and in every read filter, so a derivation that
fails on a subset silently produces unqueryable rows.

Measured over all 330 stacks: a split rule (optional zone prefix, then a
trailing stage token) maps 317 (96.1%), a declared literal maps the 12
stage-less `default` stacks to `global`, and 1 stack is `unknown` (a 2022 stack
named `….QA.yaml`). The split reproduces ol-infrastructure's
`StackInfo.env_suffix` and `namespace` (`lib/pulumi_helper.py:14`) on all 317,
and differs only where `env_suffix` would have emitted `default` or `yaml` as an
environment. The `ol.mit.edu/environment` label agreed with the name on all 141
stacks that carry it; 82 stacks carry neither tag nor label, so the name is
their only signal.

The derivation is an ordered list of rules in the org's config; witan-infra
ships no MITODL parsing:

```toml
[infra.environment]
unknown = "unknown"

[[infra.environment.stack_rules]]
match = '^default$'
environment = "global"

[[infra.environment.stack_rules]]
match = '^(?:(?P<zone>[^/]+)\.)?(?P<stage>[A-Za-z]+)$'
zone_separator = "."
case_insensitive = true
stages = { ci = "ci", qa = "qa", production = "production", dev = "dev", staging = "staging" }

[[infra.environment.evidence]]
label = "ol.mit.edu/environment"
match = '^(?P<stage>ci|qa|production|dev|rc|staging)$'
```

- Named groups are the contract: `stage` (mapped through `stages`) and `zone`
  (split on `zone_separator` into the recursive Zone path).
- A captured stage outside `stages` gives `unknown`, never the raw token.
- `unknown` is first-class: the node is written, its raw stack name is kept,
  and coverage reports the count and names. It is never guessed.
- `evidence` rules never set the value. A disagreement becomes a coverage row.
  An evidence rule may set `override = true` for a legitimate per-resource
  stage (the `mitx-staging` deployments are meant to carry a `staging` label).
  An override still has to produce a key of `stages`, or the resource gets
  `unknown`; that is why MITODL's list includes `staging` although no stack
  name carries it.

The AWS `Environment` tag in MITODL names a network plus a stage (e.g.
`operations-production`); its network part is a Zone, not the environment.

## 8. Secret handling: allowlist, never denylist

### 8.1 What the collector needs

`pulumi stack export` without `--show-secrets` never instantiates the secrets
provider: the diy backend reads the checkpoint object and re-marshals it
(Pulumi v3.276.0, `pkg/backend/diy/backend.go:1551`); only the
`--show-secrets` branch decrypts. Two passphrase-provider stacks exported with
no passphrase, and all 8,291 secret values in the exports are ciphertext.

So the collector needs read on the state bucket and nothing else:
`s3:ListBucket` on the bucket with `s3:prefix` like `.pulumi/stacks/*`, and
`s3:GetObject` on `.pulumi/meta.yaml` and `.pulumi/stacks/*`. No `kms:*` on the
secrets-provider keys, no passphrase, no write. Two things are unverified and
are checked before the CronJob ships: whether the bucket's default encryption
is a customer-managed KMS key (then `kms:Decrypt` on that key, constrained by
`kms:ViaService` to S3), and a CloudTrail check that the export window shows no
KMS `Decrypt` by the exporting identity.

The collector reads the checkpoint object directly with `boto3` (taking
`checkpoint.latest`, as the diy backend's export does, handling the `.json`,
`.json.gz` and `.json.zst` variants and refusing any checkpoint version other
than 3). It does not need the Pulumi CLI in the image.

### 8.2 Plaintext in state, regardless of encryption

The scan found live-looking plaintext credentials in IaC state, outside any
secret wrapper, in several resource classes. The inventory is not published
here; it goes to the IaC owners privately, not into this public repository.
Two consequences for this design:

- Read access to the state bucket is credential access. The collector's
  identity is a dedicated, read-only role bound to one service account, with no
  other permissions. It must not reuse a CI policy that grants write.
- A denylist of "fields that look secret" would have missed some of these, so
  the collector keeps a field only when its (resource type, field) pair is
  listed.

### 8.3 The allowlist

Record level, every type: `urn` (parsed), `type`, `custom`, `parent`,
`dependencies`, `propertyDependencies` (structure only), `protect`,
`retainOnDelete`, `external`, `created`, `modified`, the URN part of
`provider`, `sourcePosition` only when its scheme is `project://`, and
property-name lists from `additionalSecretOutputs`, `ignoreChanges`,
`replaceOnChanges`. Never: `inputs` (property names may be kept as a set for
`value_present`), `stackTrace`, `initErrors`, `__inputs`, `__meta`,
`__pulumi_raw_state_delta`, and every field the per-type table below does not
list.

`outputs` and `id` are kept only for listed types, per type. The starting
table:

| Type or family | Outputs retained |
|---|---|
| `aws:rds/instance`, `aws:rds/cluster` | `arn`, `identifier`, `address`, `endpoint`, `port`, `engine`, `engineVersionActual`, `instanceClass`, `dbSubnetGroupName`, `vpcSecurityGroupIds`, `multiAz`, `storageEncrypted`, `publiclyAccessible`, `deletionProtection` |
| `aws:elasticache/replicationGroup` | `arn`, `id`, `primaryEndpointAddress`, `readerEndpointAddress`, `port`, `engine`, `engineVersionActual` |
| `aws:s3/bucket`, `BucketV2` | `arn`, `bucket`, `region`, `bucketDomainName` |
| `aws:iam/role`, `aws:iam/policy` | `arn`, `name`, `path`, `managedPolicyArns`; policy documents parsed into principal, action and resource edges, raw JSON dropped |
| `aws:ec2/vpc`, `subnet`, `securityGroup`, `securityGroupRule` | `id`, `arn`, `vpcId`, `cidrBlock`, `availabilityZone`, `name`; rule protocol, ports, CIDRs, referenced group ids |
| `aws:ec2/launchTemplate`, `instance` | `id`, `arn`, `name`, `imageId`, `instanceType`, `iamInstanceProfile`, `vpcSecurityGroupIds` |
| `aws:eks/cluster` | `arn`, `name`, `endpoint`, `version` |
| `aws:route53/record` | `name`, `type`, `zoneId`, `fqdn`; `records` only for A, AAAA, CNAME, alias |
| `kubernetes:*` | `apiVersion`, `kind`, `metadata.name`, `metadata.namespace`, `metadata.labels`, annotation `eks.amazonaws.com/role-arn` |
| `kubernetes:apps/v1:*`, `batch/v1:*` | container `image`, `serviceAccountName`; env as name plus `value_present`, `valueFrom` refs as `value_ref`; secret and configMap volume names |
| `kubernetes:core/v1:Service` | `spec.type`, `spec.ports`, `spec.selector` |
| `kubernetes:core/v1:Secret`, `ConfigMap` | name, namespace, data key names with `value_present` |
| `kubernetes:helm.sh/v3:Release` | `name`, `namespace`, `chart`, `version`, `repositoryOpts.repo`, `status.status` |
| Vault secret CRDs, `vault:generic/secret`, `vault:kv/*` | mount and path as `value_ref`; `value_present` |
| `vault:database/secretBackendConnection` | `name`, `backend`, `pluginName`, `allowedRoles`; host/port/db only when the URL userinfo is a `{{...}}` template |
| `pulumi:pulumi:StackReference` | `name` only, as a `StackDependsOn` edge |
| `pulumi:pulumi:Stack` | output key names with `value_present`; values only for `*_arn`, `*_id`, `*_name`, `*_endpoint`, `*_address`, `*_port`, `*_region`, `*_bucket` that pass the shape guard |
| `pulumi:providers:*` | `version`; AWS `region` and `assumeRole.roleArn`; Kubernetes cluster `server` URL |
| anything else | skeleton only, counted as `collector_unlisted_type` in coverage |

Extending the table is a reviewed change, made only after a scan of the current
corpus shows the new field is an identifier.

Every retained string also passes a shape guard: drop the field (and count it,
never log it) if it contains a secret sigil, `://` with literal userinfo,
`-----BEGIN`, an AWS access key id pattern, a `Basic`/`Bearer` header, or is
longer than 512 characters. A guard hit on an allowlisted field means the table
is wrong, and alerts.

The raw export never touches disk: one stack at a time in memory (15.5 MB
maximum), `readOnlyRootFilesystem`, no core dumps, pydantic models over raw
records set `hide_input_in_errors=True`, logs carry type, URN and field name
only. Only filtered records leave the process; nothing raw is cached or
journaled.

## 9. Collectors

### 9.1 Protocols and registration

`BatchCollector` and `LiveProbe` are `runtime_checkable` Protocols, following
`witan.scan.Scanner` (`mcp/servers/witan/witan/scan/models.py:59`) and
`mcp/servers/witan/examples/example-scanner-plugin/`. Each declares
`Capabilities`: `tier`, `emits`, `requires` (credential slots; a collector never
reads an env var itself), `freshness`, `cost`, `reap`, and `completeness`.

Registration is by entry-point groups `witan_infra.collectors`,
`witan_infra.live_probes`, `witan_infra.transforms` and
`witan_infra.secret_providers`, plus dotted paths in config. The built-in
collectors register through the same groups, so the plugin path is the one we
run every day and a defect in it surfaces immediately. A collector that fails to
import is recorded as `unavailable(reason)`, never fatal. `witan infra
collectors` lists every discovered collector, whether it is enabled, why not,
its declared capabilities and its `check()` result. Today the workspace
declares one entry-point group (`witan.scanners`) and reads another
(`witan.classifiers`, `witan/screen/classifier.py:23`) that no package declares.

### 9.2 Observations carry raw attributes

A collector returns `Observation`s whose attribute keys are the source's own
spelling. It fills a semantic slot only when the source provides it
structurally (the Kubernetes API gives `namespace` positionally), never when it
arrives as a named attribute whose spelling differs per org. Mapping raw keys
to slots is a config-driven layer the collector cannot see: per slot, an
ordered list of candidate source keys, OTel semantic conventions by default,
org keys prepended. Value transforms: `repo_uri` (which is
`witan_core.repo_key.normalise`), `lower`, `strip_prefix`, `regex`, `map`.

Credentials resolve inline, then `_env`, then `_file`, then a `SecretProvider`
plugin, then the collector's ambient default. The zero-config case works end to
end on defaults.

### 9.3 Completeness governs what absence means

| `completeness` | Absence means | May reap |
|---|---|---|
| `authoritative` | the object is gone from this scope | yes, under the clauses in [10.4](#104-when-a-run-may-reap) |
| `sampled` | possibly a scrape gap | no; marks its sighting missing (`miss_count` counts the runs), never drops it |
| `partial` | nothing | no; strictly additive, never marks or ages anything |

Operators may downgrade a collector's completeness in config, never upgrade it.

### 9.4 The collectors, in the order they land

- IaC state (`authoritative`, scope = stack). Pulumi first: scopes from the
  stack list in the backend, each stack's last-update time as the watermark.
  `StackReference` records become `StackDependsOn`; `sourcePosition` becomes
  `iac_repo`/`iac_path`/`iac_line`.
- Kubernetes (`authoritative`, scope = cluster). The first collector that
  overlaps another, so the per-type `authority` order decides, field by
  field, which collector's sighting a projected object takes its value from.
  Supplies Helm- and `ConfigGroup`-applied objects that IaC state lacks.
- Metrics inventory (`kube_*_info`, `sampled`): the fallback when the
  Kubernetes API is unreachable. Cannot give container env, ingress specs or
  args, so it degrades bindings and routes.
- Trace service graph (`partial`): edge existence only. The backend's synthetic
  peers (e.g. `client="user"` for uninstrumented ingress) are discarded through
  a config list.
- Span attributes (`partial`): `db.system`, `db.name`, `messaging.destination`,
  `server.address`, `http.route`, windowed, with a confidence score.
- Cloud inventory (`authoritative` per account/region): ingest only resources
  already in IaC state, types on a configured allowlist, or resources
  referenced by something modelled. No "describe the whole account" sweep.
  Joins to declared resources on provider-native id. Ships as an extra
  (`witan-infra[aws]`) to prove the third-party collector path on ourselves.

## 10. The write path

### 10.1 One writer, one run at a time

One reconciler writes the graph, and only one run of it at a time. A run takes
a lease first: a `SyncLease` row (key `reconciler`, holder, `expires_at`)
claimed by compare-and-swap, the mechanism task claims use
([ADR 0003](../../explanation/decisions/0003-atomic-task-claims-cas.md)), and
renewed while the run lasts. The scheduled CronJob, `witan infra sync` and the
`infra_sync` tool all go through it, and the CronJob also sets
`concurrencyPolicy: Forbid`.

The lease is not enough on its own: ADR 0003 itself leaves a window in which
two claimants can both believe they hold a claim, and a run that stalls past
`expires_at` would keep writing after another run took over. So the destructive
write is fenced as well. Immediately before its deletes, a run re-reads its
lease with `read_with_commit`, confirms it still holds it, and issues the
deletes through `change_many(if_commit=<that commit>)`, the precondition task
writes use (`_fence_on`, `witan/server.py:1216`). Any write by another run in
between moves the head, the deletes are refused with nothing written
(`OmnigraphConflict`), and the run aborts. `change_many` refuses `if_commit` on
a chunked batch, so if the deletes need chunking each chunk is fenced on the
commit the previous chunk produced. Merges go through `load`, which takes no
precondition; they are idempotent upserts, so a stale run's merge can at worst
re-add rows that the next run's diff removes.

Collectors run sequentially within a run, with parallelism inside a collector;
N concurrent writers on one Lance store is contention plus fragmentation.
`mode="overwrite"` is never used (it truncates the node type). Batches go
through `witan_core.chunking` and a ported `_dedupe` (first occurrence wins on
node keys and edge keys), because `load` rejects a whole batch on one `@unique`
violation, including a key repeated within the batch.

### 10.2 Per-source sightings, projected objects

Two collectors can describe the same object (the IaC collector and the
Kubernetes collector both see a Deployment), and a merge replaces a whole row.
If both wrote the `Resource` row directly, each run would null the other's
fields. So collectors never write object rows. The write model has two layers:

- `Sighting` rows, one per (collector, scope, object id): what that collector
  saw in that scope, after the allowlist and attribute mapping, plus
  `first_seen`, `last_seen`, `missing_since` and `miss_count`. Each sighting
  belongs to exactly one collector and scope, so no two writers share a row.
- Object rows (`Resource`, `Service`, `Zone`, `InfraBinding`, `Alias`) and
  relation edges, projected by the reconciler from all live sightings: each
  field from the highest-authority sighting that carries it (the per-type
  authority order in config), `presence` from which kinds of collector saw it,
  `first_seen` the earliest, `missing_since` set only when every authoritative
  sighting is missing.

Attribution ([6.2](#62-the-attribution-ladder)) is part of the projection and
is recomputed over the whole projected `Resource` set on every run, not per
scope: the `alias_identity`, `image_repo` and `name_token` rungs read resources
in other stacks, and the manual map and ladder are config that can change
without any stack changing.

A relation edge whose endpoint is not in the projected node set is dropped and
counted as `dangling_reference` in coverage. That covers the cross-scope case:
when an object is removed, edges other scopes or `partial` collectors hold
against it go with it, because the object no longer exists anywhere. A
`partial` collector's rule ("absence never deletes") is about its own
observations disappearing, not about its endpoint being removed by an
authoritative source.

### 10.3 A run is a diff over a full read

The graph is small enough to read whole every run: about 36k object nodes plus
sightings and on the order of 100k edges, one read request per type (about 15
types). A full read of the largest type took 0.24 s locally; the deployed cost
of a read at that size is unmeasured, but deployed reads measured so far are
dominated by the round trip ([5.6](#56-size-and-read-cost)). So the reconciler
computes the desired graph in Python and writes the difference:

1. Take the lease.
2. Read the current graph: every infra node and edge type, each read checked
   against a `count()` read, failing loudly on a mismatch.
3. List each collector's scopes and decide which to collect: a scope whose
   watermark changed (the source's last-modified time, the collector version,
   or the hash of the config that shapes its sightings), or whose latest
   `SyncRun` is not `complete`. Write a `SyncRun` row with `status = running`
   for each. Unchanged scopes are not fetched, which on a typical run is most
   stacks.
4. Build new sightings for the collected scopes, and carry every other scope's
   current sightings forward. Missing sightings age on every run, collected or
   not: an unchanged watermark means the source still omits the object, so a
   skipped scope's missing sightings advance exactly as a collected scope's do.
   For each sighting missing from its scope's latest complete snapshot: under
   an authoritative collector that passes
   [the reap gate](#104-when-a-run-may-reap), set `missing_since` (if unset),
   increment `miss_count`, and drop the sighting once
   `miss_count >= reap_min_cycles` and `missing_since` is older than
   `retention_days`; under `sampled`, mark it missing but never drop it; under
   `partial`, leave it unchanged. A scope that has disappeared from a complete
   scope listing (a deleted stack) is treated as fully absent under the same
   rule. Reappearance resets `missing_since` and `miss_count`, so flapping
   costs nothing.
5. Project objects and edges from all sightings and compute attribution. The
   result is the desired state of the projected types (`Resource`, `Service`,
   `Zone`, `InfraBinding`, `Alias`, `UnresolvedIdentity`,
   `ExternalDependency`, their edges) and of `Sighting`.
6. Write, merge first and delete second: `load --mode merge` every new or
   changed row, then the fenced deletes ([10.1](#101-one-writer-one-run-at-a-time))
   of every row of those types that is in the current graph and not in the
   desired state, by key (`slug` for nodes, `edge_key` for edges).
   `SyncRun` and `SyncLease` are bookkeeping, never part of the diff; `SyncRun`
   rows beyond the last few per (collector, scope) are pruned by key in the
   same fenced commit.
7. Set each `SyncRun` to `complete`, last, and release the lease.

Readers never see an object missing between steps 6's two halves: they see the
old and new rows together for a moment. Deletes and inserts cannot share a
commit in omnigraph 0.15, which is why the August plan's "delete the slice,
then reinsert" order had a visible empty window and is not used. Branches are
not needed for atomicity.

Watermarks trust only the latest `complete` `SyncRun` for a (collector, scope).
A run that dies leaves `running` rows, so the next run re-collects those scopes
and does not skip them; and because the lease expires, a dead run cannot block
the next one forever.

Hard deletion is the reconciler's step 6, nothing else's. `witan_core.maintenance`
only throttles the Stop-hook `optimize` and never fires for `s3://` or `https://`
stores (`REMOTE_PREFIXES`, `maintenance.py:25`); whether the deployed store
needs a scheduled `optimize` is decided with the CronJob in Phase 5.

The first test of Phase 2 confirms that `delete … where <key> in $keys` with a
list parameter works at batch sizes the reconciler needs (the omnigraph 0.15
docs list `in` among delete predicates; it has not been run, at any size). If it
does not, the
verified fallback is the run-stamped form: merge every desired row stamped
`sync_run = <run>`, then `delete … where sync_run != $run` per type
(memory `pat-merge-then-reap-replace-a-graph-slice-on-omnigra-29430c`), at the
cost of rewriting every row each run.

### 10.4 When a run may reap

A sighting is marked missing or dropped (step 4 above) only when all of these
hold for the collector and scope, modelled on
`witan_code/indexer.py::_may_purge` (`:901`):

1. `scope.is_full`: the run covered the whole scope, not a filtered subset.
2. `result.complete and not result.errors`.
3. `completeness == "authoritative"` (or `sampled`, for marking only).
4. `caps.reap != "never"`.
5. `owns_scope(collector, scope)`. Advisory: Cedar in omnigraph 0.15 scopes
   authority to graph and branch only, with no node-type or row predicate
   (`omnigraph-policy/src/lib.rs:913-950`, deferred per `lib.rs:990-1009`;
   read from source, not exercised against the binary). The ingester token can
   write every row, so this clause holds only for honest clients, and the code
   says so where it is defined.
6. For a deleted scope: the scope listing itself was complete.

Ownership between collectors needs no clause of its own any more: a collector
can only drop its own sightings, and an object survives as long as any live
sighting holds it.

### 10.5 Tests that matter more than the feature

- A `partial` collector returning empty produces zero dropped sightings and
  zero `missing_since` marks.
- A truncated `authoritative` result produces zero drops.
- A kill at any step leaves every touched scope re-collectable and nothing
  deleted that the last complete run did not intend.
- Two runs started together: the second fails to take the lease and exits
  without writing. A run whose lease was taken over mid-run has its deletes
  refused and aborts.
- A stack that drops a resource and then stops changing still has the
  resource's sighting dropped after `reap_min_cycles` runs and
  `retention_days`, without the stack being re-collected.
- The IaC and Kubernetes collectors both seeing a Deployment, then one of them
  losing it: the `Resource` survives, with its fields from the remaining
  sighting.
- A changed manual map re-attributes resources in stacks that did not change.
- `--dry-run` prints the sightings, the projected diff and the deletes without
  writing. A bad reap deletes a production map.

## 11. Reads, and batch existence with live magnitude

### 11.1 Read shape

omnigraph 0.15 has no read batching: one `match`/`return` per query, one query
per HTTP request, and no batch route. Multi-hop matches do work: a 4-variable
match across `ComposedOf` and `DeclaredDependsOn` returned correct rows, as did
a 6-variable chain and a variable-length `{1,4}` hop (which follows one edge
type and terminates on cycles). So:

- each tool does one multi-hop query where it can, with concurrent reads pinned
  to one snapshot where fan-out is unavoidable;
- every projection is aliased (`$s.slug as service`), because
  `witan_core`'s `_read_rows` strips the variable prefix and silently collapses
  same-named columns;
- an unbounded walk that alternates edge types (service to resource to
  dependent resource to owning service, repeatedly) uses `export` of the
  relevant types plus a Python BFS over the denormalised fields.

### 11.2 The batch/live rule

The graph holds what changes on deploy timescales: existence, identity,
topology, declared config keys. Anything that changes faster than the sync
interval (request rates, error rates, replica counts, health) is fetched live,
at read time, by `LiveProbe`s, and never stored. The schema has no numeric
measurement field, so there is nowhere to store a rate even by accident.

Live fetches share one deadline for the whole tool call (not per-probe
timeouts, so N probes cannot make an N×timeout call), with a TTL cache keyed by
probe, version and canonical request, mandatory negative caching, and a
per-probe circuit breaker. Fan-out reuses the ordered-parallel shape of
`witan_code.store.map_refs` (`witan_code/store.py:962`, 8 workers). A probe
failure never raises out of a tool.

An unavailable live signal renders as `null` plus a `degraded` entry, never as
`0`. `request_rate: 0` says the service is dead; `request_rate: null` with a
`degraded` entry says we do not know.

Whether probes run server-side is decided explicitly at Phase 7: server-side
is faster and centralises credentials, but gives every MCP caller mediated read
access to the observability backend. The `runtime` block on `infra_component`
is re-evaluated there too; if the live join proves flaky it is dropped, because
the declarative map is the durable value.

## 12. Tool surface

Five tools, no thin wrappers (see the surface-refinement spec's
[tool consolidation](witan-surface-refinement-spec.md) for why):

| Tool | Purpose |
|---|---|
| `infra_find(query, kind, environment, repo, evidence, limit)` | locate and browse; returns a dict so `coverage` cannot be dropped |
| `infra_component(component_id, environment, evidence)` | one read: workloads, routes, 1-hop dependencies, config key names, declaring IaC file and line, and a `runtime` block (null until Phase 7) |
| `infra_impact(component_id \| repo, direction, max_depth, kinds, evidence)` | transitive walk plus `public_surface`, the externally reachable routes reached; named to rhyme with `code_impact`; `repo` as a root is a one-call shortcut |
| `infra_coverage(environment)` | what is collected, from where, how fresh, and what is missing; the analogue of `code_indexed_repos` |
| `infra_sync(source, environment, force)` | `@mcp.tool(task=TASKS_ENABLED)`, like `code_reindex` |

Conventions: the component id is one spelling everywhere, and the output field
is `component_id`, matching the input parameter so results round-trip. Config
key names in `infra_component` use the bridge's `env_var` contract-key
vocabulary so an agent can pivot to `code_interface_consumers` without
translation; that needs `normalize_key`/`normalize_endpoint` moved from
`witan_code/bridge_extractors.py` into `witan_core.contract_key` first. The
result envelope (`provenance` per fact, `coverage` per result, `degraded`)
ships from the first tool, so its shape never changes.

Three deliberate departures from the surface-refinement conventions, each
stated in the server instructions:

- `evidence` is a set filter (`declared`, `observed`), not a minimum tier,
  because the two are not ordered: either can hold what the other misses.
- `repo` is an explicit filter and is never auto-detected. Silently scoping
  "what runs in production" to the caller's checkout is a confidently wrong
  answer.
- No infra read returns bare `None`.

Mechanics: `ObservabilityMiddleware` added first so it is outermost,
`caching.hint_kwargs()`, pagination and limits from the start. Remote calls go
through a subclass of `witan_core.remote.proxy.RemoteMCPProxy`, which already
requires keyword arguments.

## 13. Joining to code and memory

### 13.1 Two code joins, both declared

- "Declared here": `Resource.iac_repo` + `iac_path` + `iac_line`, from
  `sourcePosition` resolved against the directory holding the stack's
  `Pulumi.yaml`. Composes directly into a witan-code `CodeFile` slug in the IaC
  repo.
- "Built from": `Service` to application repo, from the org's manual map,
  corroborated by IaC-declared anchors in state (container image repositories,
  VCS provider resources).

The runtime repository attribute is corroboration only. During scoping in
August, Tempo returned no values for the
`resource.ol.mit.edu/source_repository` attribute, and where the
Kubernetes label is set, ol-infrastructure's `_sanitize_label_value` strips the
scheme, maps `/` to `_` and `:` to `-` and truncates to 63 characters, which is
lossy and not invertible. In state it covers 20 repos and joins to 7 of the 316
GitHub repositories declared there. Anchoring on the declared side is also the
more portable choice.

witan-infra stores repo URIs as strings produced by `witan_core.repo_key.normalise`.
It imports neither sibling package; `symbol_id` to repo is
`split("#", 1)[0]`.

### 13.2 The `deployment` enrichment on `code_impact`

`code_impact` and `code_cross_repo_impact` gain a `deployment` key beside
today's `root`, `impacted` and `truncated` (`witan_code/server.py:612`): repos
to services to depth-1 downstream to `public_surface`, with `coverage` and a
literal `next_call` template. Hard node cap, about 300 ms budget, and the key is
omitted entirely on timeout or when witan-infra is absent (no `"deployment":
null`). The kill switch is config, not a tool parameter.

This is the main bet on discoverability: a field in a result the agent already
asked for arrives at the moment it is relevant and costs nothing to read. How
witan-code reaches witan-infra to fill it is the first [open decision](#16-open-decisions).

### 13.3 Memory

`recall(component_id=...)` is a new seed beside the existing `symbol_id`,
`task`, `topic`, `contract` and `memory` seeds, implemented entirely in Layer 1: it
resolves `Topic{kind: "entity", name: <component_id>}` and walks the topic's
memories, like the existing `topic` seed. `entity` is already an accepted topic
kind (`TopicKind`, `witan/server.py:634`), and topic resolution splits on the
last colon (`_resolve_topic`, `:1415`), so colons inside a component id are
safe. Memories attach through the existing `Tagged` edge. Topics are anchored
only at environment-independent ids (`service:…`, `datastore:…`), never
`…@production`, to keep topic cardinality low.

## 14. Authorization

`infra` gets its own Cedar bundle, `infra.policy.yaml`, bound with
`applies_to: [infra]` whenever the graph is declared. The binding is a
confidentiality control as well as a write gate: in omnigraph 0.15 a graph
bound to no bundle gets no policy, and (read from the server source, not
exercised against the binary) `read` on it is allowed for any
static-token actor (`omnigraph-server/src/settings.rs:262`,
`handlers.rs:675-716`). The `build_cluster_policies` docstring, which calls an
unbound graph default-deny, is corrected when `infra` is added, and a test
asserts every declared graph appears in some bundle's `applies_to`.

| Group | Actions on `infra` |
|---|---|
| `witan-infra-readers` (a subset of humans) | `read`, `invoke_query`; not `export` |
| `witan-infra-ingester` (`svc-witan-infra`) | `read`, `invoke_query`, `change` on protected `main` |
| `witan-service` | `read`, `schema_apply` |
| `witan-admin` | `read`, `invoke_query`, `schema_apply`; no `change` (the fix for a bad row is a resweep) |

No branch actions: the graph is single-branch. Readers are narrower than
`witan-users` because the graph carries hostnames, ARNs, account ids and config
key names; reads leave the MCP tier under the caller's own token
(`witan/server.py:283-310`), so the grant is a real per-user boundary.

The new groups go into `render_groups.py`'s `SERVICE_ACTOR_GROUPS`/`KNOWN_GROUPS`,
which is where an unprovisioned group is dropped with its rules instead of
rendered empty (the empty-group crash-loop guard, `render_groups.py:172-194`).
By the policy crate's source, a bundle whose groups are all unprovisioned still
compiles and denies everyone; `policy/check.sh` exercises that case once the
bundle exists.
How the reader group gets its membership is an [open decision](#16-open-decisions).

Row-scoped write authority is not expressible ([10.4](#104-when-a-run-may-reap)). If
per-cluster collectors appear later, they hand snapshots to the reconciler,
which stays the only writer and enforces scope against the submitter's
authenticated identity, rather than each collector writing the graph.

## 15. Discoverability

- No UserPromptSubmit hook for infra. The measurement for the code layer,
  recorded on this project during scoping, was 66 injections producing zero
  `code_*` calls and zero tool searches.
- The `deployment` enrichment block is the primary channel ([13.2](#132-the-deployment-enrichment-on-code_impact)).
- Server instructions carry the honesty rule near the top: never conclude
  "nothing depends on X" without reading `coverage`. `FastMCP.mount()` carries
  tools, not instructions, so the umbrella appends a mounted server's
  instructions to its own (`mount_witan_code`, `witan/cli/_common.py:49-66`);
  witan-infra mounts the same way.
- A `witan-infra` skill whose description leads with the questions it answers,
  with examples marked as one org's instantiation.
- A predefined kill criterion, instrumented from the first release: count
  `infra_*` calls per session, `code_impact` results carrying a `deployment`
  block, and `infra_*` calls that follow one. If the block appears and produces
  no follow-up, the value is in the CLI and the enrichment alone, and the answer
  is not a prompt block.

## 16. Open decisions

Each is the owner's call; the spec is written to the recommendation.

1. How `code_impact` reaches witan-infra for the `deployment` block. Options:
   (a) witan-code imports witan-infra optionally, the best-effort pattern of
   `_code_server()` (`witan/server.py:8898`), making the DAG
   `witan_core <- witan_infra <- witan_code`; (b) a FastMCP middleware in the
   umbrella `serve` that post-processes results by tool name; (c) a
   `ResultEnricher` protocol in `witan_core` and an entry-point group that
   witan-code calls and witan-infra registers into. I recommend (c): neither
   server imports the other, the enricher is a plain function testable on its
   own, and it uses the same entry-point mechanism witan-infra's collectors
   already require. (a) couples a hardened package to a new one; (b) is opaque
   string matching on tool names.
2. Who reads the `infra` graph. `render_groups.py` today can only put every
   `act-*` actor into `witan-users`; a narrower `witan-infra-readers` needs a
   membership source. I recommend a Keycloak group synced by the existing token
   sync into a second membership map, read by a new flag on `render_groups.py`.
   Until it exists the group is unprovisioned and only the service and admin
   actors can read, which is the safe default.
3. The MITODL service set and synonyms ([6.3](#63-the-service-set-is-config)):
   57 services and 18 synonyms as measured. I recommend accepting it as the
   first config instance and letting the attribution test in Phase 4 flag
   changes.

## 17. Task DAG (ratified, with changes)

The phase tasks under the epic stand. Changes from the August plan:

- Closed before this spec: five of the six gating spikes (attribution hit rate,
  secret handling, omnigraph engine limits, graph size, environment
  derivation); the `witan_code.chunking` promotion (already in `witan_core`
  via "fix(witan-code): pack an edit flush's nodes and edges into one load",
  #608); the atomic-slice-swap spike (made unnecessary by writing merges before
  deletes, [10.3](#103-a-run-is-a-diff-over-a-full-read)); and
  the `FastMCP.mount()` instructions question (answered and fixed by "trim tool
  schemas and annotate every tool", #661).
- The Cedar bundle spike is answered ([Authorization](#14-authorization)) but
  stays open on open decision 2, who reads the graph. It blocks only Phase 5.
- The schema-evolution spike (adding an enum variant on a live graph) no longer
  blocks anything, because the infra schema has no enums.
- The import-direction task is resolved by open decision 1.
- Attribution moves from Phase 4 to Phase 2. The IaC-sourced rungs need nothing
  from Kubernetes, the tools in Phase 3 need `ComposedOf` to answer anything
  by service, and the reconciler recomputes attribution every run anyway.
  Phase 2 re-runs the attribution measurement as a test over real exports
  before it merges. Phase 4 adds the Kubernetes-sourced rungs and the identity
  ladder for objects two collectors both see.
- Phase 5 (provisioning in ol-infrastructure) carries the Cedar bundle, the IAM
  policy from [8.1](#81-what-the-collector-needs), the bucket-encryption and
  CloudTrail checks, `concurrencyPolicy: Forbid`, and the decision on a
  scheduled `optimize` for the deployed store.

| Phase | Delivers | Blocked by |
|---|---|---|
| 0 | package skeleton, config model, local store, `infra-schema.pg`, `witan infra status` | this spec |
| 1a | collector protocols, entry-point registry, attribute mapping, `witan infra collectors` | 0 |
| 1b | IaC state collector (Pulumi via boto3), allowlist, sightings, `--dry-run` | 1a, Terraform parity spike |
| 2 | reconciler: lease, `SyncRun`, watermarks, projection, IaC attribution, diff write, reap gate | 1b, `normalize_key` promotion |
| 3 | the five `infra_*` tools, graph-backed | 2 |
| 4 | Kubernetes collector, identity ladder, Kubernetes attribution rungs | 3, Kubernetes access spike |
| 5 | `infra` graph, bundle and sync CronJob in ol-infrastructure | 3, Cedar bundle spike (open decision 2) |
| 6 | sampled and partial observed-topology collectors | 4 |
| 7 | live-read path | 3 |
| 8 | cloud inventory and the undeclared delta | 4 |
| 9 | `deployment` enrichment, recall seed, skill, CLI | 3, open decision 1 |
| follow-on | retire IaC source scraping from witan-code's bridge | 5 |

Phases 0 to 4 land in agent-kit alone, before anything needs an
ol-infrastructure deploy.
