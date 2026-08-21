# Guides

Task-oriented pages: you already know what you want to do, and you want the
steps. If you are still orienting, start with [Get
started](../getting-started/index.md) instead.

## Using witan

<div class="grid cards" markdown>

-   **[witan user guide](witan-user-guide.md)**

    The full day-to-day loop for the coordination graph — memory, tasks,
    projects, sessions, and the operating modes the store can run in.

-   **[witan-code user guide](witan-code-user-guide.md)**

    Indexing, querying, and the cross-repo bridge, in depth.

-   **[Branch indexing](branch-indexing.md)**

    How per-branch views work, who is allowed to write the default view, and
    how idle views are reaped.

</div>

## Operating it

<div class="grid cards" markdown>

-   **[Using a deployed witan](deployed-witan.md)**

    Point your local CLI and agent at a shared service: OIDC login, target
    configuration, and what changes when the store is no longer on your disk.

-   **[Write-path scanning](write-path-scanning.md)**

    Every write is scanned for secrets and PII before it persists. How
    enforcement is configured, how to suppress a false positive, and how to add
    a detector.

-   **[Migration runbook](migration-runbook.md)**

    Moving a store: local to shared, format upgrades, and reconciling two stores
    that both have writes.

</div>

!!! info "These pages live with the code"

    Every guide here is mirrored from the package it documents, so it stays in
    step with the release rather than drifting into a second, slowly-wrong copy.
    Each page links to its authoritative source at the top — edit there.

## Things people commonly need

| I want to… | Where |
| --- | --- |
| Change where the graph is stored | [`WITAN_MEMORY_URI`](../reference/environment.md#store-and-attribution) |
| Route work repos and personal repos at different stores | [Named targets](witan-user-guide.md) |
| Stop a detector flagging a false positive | [Write-path scanning](write-path-scanning.md) |
| Run the code indexer in CI | [`WITAN_CODE_CI_REPOS`](../reference/environment.md#ci-code-graph-indexer) |
| Understand why a claim was rejected | [Coordinating work](../explanation/task-coordination.md) |
| Tune what `recall` returns | [`WITAN_RANK_*`](../reference/environment.md#recall-ranking) |
