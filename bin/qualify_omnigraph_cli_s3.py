#!/usr/bin/env -S uv run --quiet --package witan-core python
"""Qualify the patched omnigraph CLI against a copy of a cluster root on S3.

The omnigraph-server image's CLI is built from a backport of upstream PR #1189
(docker/patches/omnigraph-cli-inline-s3-root.patch) so the data tier can
address its S3 cluster root with the server stopped. Upstream showed apply,
status and force-unlock against an S3 root; this drives the full set the image
relies on (boot converge, maintenance, unlock) against a root that already
holds data, the way a deployed root does:

1. Deploy a small cluster to a LOCAL root and load several batches into it,
   so its datasets carry multiple versions and fragments.
2. Copy that root to S3 (``aws s3 cp --recursive``). That copy is what gets
   qualified.
3. Against the copy, with an inline ``s3://`` target: status, plan (must see
   the copy as converged), apply of a config that adds a graph (a real write,
   as boot converge does after an image bump), status and replay by
   deployment id with ``--writers-stopped``, force-unlock, optimize (must
   write), cleanup (refused without ``--yes``, then run with it, must remove
   objects), force-unlock after each. Every retained lock must be released by
   its exact id and nothing may be left locked.
4. Copy the root back and query it locally: every loaded row must survive
   maintenance.

It also checks the patch's boundary: a data command (``query``) against the S3
root still refuses.

The AWS destination comes from the environment (``AWS_REGION``,
``AWS_ENDPOINT_URL_S3``, credentials), which the patched CLI captures once
per call and the ``aws`` CLI reads the same way.
"""

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import cyclopts

app = cyclopts.App(
    name="qualify-omnigraph-cli-s3",
    help="Drive the patched omnigraph CLI against a copy of a cluster root on S3.",
)

ACTOR = "act-qualify"
GRAPH = "knowledge"
BATCHES = 4
ROWS_PER_BATCH = 25

SCHEMA = """node Person {
  name: String @key
  age: I32?
}
"""
QUERIES = """query find_person($name: String) {
  match { $p: Person { name: $name } }
  return { $p.name, $p.age }
}

query everyone() {
  match { $p: Person }
  return { $p.name }
}
"""
POLICY = f"""version: 1
groups:
  operators: [{ACTOR}]
rules:
  - id: operators-change
    allow:
      actors: {{ group: operators }}
      actions: [read, change, schema_apply]
"""
# Added to the config before the apply on S3, so that apply has a graph to
# create there rather than converging a root that is already converged.
ADDED_GRAPH = "archive"


def _cluster_yaml(graphs: list[str]) -> str:
    declared = "".join(
        f"  {graph}:\n"
        "    schema: ./people.pg\n"
        "    queries:\n"
        "      find_person:\n"
        "        file: ./people.gq\n"
        for graph in graphs
    )
    return (
        "version: 1\n"
        "metadata:\n"
        "  name: qualify-s3-root\n"
        "state:\n"
        "  backend: cluster\n"
        "  lock: true\n"
        f"graphs:\n{declared}"
        "policies:\n"
        "  base:\n"
        "    file: ./base.policy.yaml\n"
        f"    applies_to: [{', '.join(graphs)}]\n"
    )


class Omnigraph:
    """The CLI under test, pinned to one target and an empty operator config."""

    def __init__(self, binary: Path, target: str, operator_config: Path) -> None:
        self.binary = binary
        self.target = target
        self.operator_config = operator_config

    def run(self, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
        argv = [
            str(self.binary),
            "--operator-config",
            str(self.operator_config),
            "--target",
            self.target,
            *args,
        ]
        print(f"$ omnigraph --target {self.target} {' '.join(args)}", flush=True)
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if ok and result.returncode != 0:
            sys.stderr.write(result.stdout + result.stderr)
            raise SystemExit(f"FAILED (exit {result.returncode}): {' '.join(args)}")
        if not ok and result.returncode == 0:
            sys.stderr.write(result.stdout + result.stderr)
            raise SystemExit(f"expected a refusal, got success: {' '.join(args)}")
        return result

    def json(self, *args: str) -> dict:
        return json.loads(self.run(*args, "--json").stdout)

    def status(self) -> dict:
        return self.json("cluster", "status")

    def unlock_retained(self, after: str) -> None:
        lock_id = self.status()["lock_id"]
        if lock_id is None:
            raise SystemExit(
                f"{after} left no lock to release; expected it to retain one"
            )
        unlocked = self.json("cluster", "force-unlock", lock_id)
        if unlocked["unlocked"] is not True:
            raise SystemExit(
                f"force-unlock {lock_id} after {after} did not unlock: {unlocked}"
            )
        print(f"  released {lock_id} after {after}")

    def assert_unlocked(self, where: str) -> None:
        lock_id = self.status()["lock_id"]
        if lock_id is not None:
            raise SystemExit(f"{where}: lock {lock_id} is still held")


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def _aws(*args: str) -> str:
    return subprocess.run(
        ["aws", *args], capture_output=True, text=True, check=True
    ).stdout


def _object_count(uri: str) -> int:
    return len(_aws("s3", "ls", "--recursive", uri + "/").splitlines())


def _write_config(directory: Path, graphs: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "people.pg").write_text(SCHEMA)
    (directory / "people.gq").write_text(QUERIES)
    (directory / "base.policy.yaml").write_text(POLICY)
    (directory / "cluster.yaml").write_text(_cluster_yaml(graphs))


def _seed(local: Omnigraph, config: Path, work: Path) -> set[str]:
    deployed = local.json("--as", ACTOR, "cluster", "apply", "--config", str(config))
    _expect(deployed["status"] == "complete", f"local seed apply: {deployed}")
    local.unlock_retained("local seed apply")
    names = set()
    for batch in range(BATCHES):
        rows = [
            {"type": "Person", "data": {"name": f"p-{batch}-{row}", "age": row}}
            for row in range(ROWS_PER_BATCH)
        ]
        names.update(row["data"]["name"] for row in rows)
        data = work / f"batch-{batch}.jsonl"
        data.write_text("".join(json.dumps(row) + "\n" for row in rows))
        local.run(
            "--as",
            ACTOR,
            "--graph",
            GRAPH,
            "load",
            "--data",
            str(data),
            "--mode",
            "append",
        )
        local.unlock_retained(f"load batch {batch}")
    return names


def _query_names(local: Omnigraph, config: Path) -> set[str]:
    result = local.json(
        "--graph",
        GRAPH,
        "query",
        "everyone",
        "--query",
        str(config / "people.gq"),
    )
    return {row["p.name"] for row in result["rows"]}


def _run(binary: Path, s3_root: str, work: Path) -> None:
    operator_config = work / "operator.yaml"
    operator_config.write_text('{"version": 1}')
    config = work / "config"
    _write_config(config, [GRAPH])
    seed_root = work / "seed-root"
    seed_root.mkdir()
    local = Omnigraph(binary, f"file://{seed_root}", operator_config)
    loaded = _seed(local, config, work)
    _expect(
        _query_names(local, config) == loaded,
        "seed root does not hold every loaded row",
    )

    _aws("s3", "cp", "--recursive", "--only-show-errors", str(seed_root), s3_root)
    print(f"copied the seeded root to {s3_root} ({_object_count(s3_root)} objects)")

    s3 = Omnigraph(binary, s3_root, operator_config)
    s3.assert_unlocked("the S3 copy before any command")
    plan = s3.json("cluster", "plan", "--config", str(config))
    _expect(plan["changes"] == [], f"the S3 copy does not plan as converged: {plan}")

    # A changed cluster.yaml, as an image bump brings to boot converge, so the
    # apply below writes to S3 instead of confirming a converged root.
    _write_config(config, [GRAPH, ADDED_GRAPH])
    plan = s3.json("cluster", "plan", "--config", str(config))
    _expect(plan["changes"] != [], f"adding {ADDED_GRAPH} planned no change: {plan}")
    applied = s3.run(
        "--as", ACTOR, "cluster", "apply", "--config", str(config), "--json"
    )
    _expect(
        "provider=S3" in applied.stderr,
        f"apply did not report an S3 direct root:\n{applied.stderr}",
    )
    deployed = json.loads(applied.stdout)
    _expect(
        deployed["status"] == "complete" and deployed["result"]["converged"] is True,
        f"apply on the S3 copy: {deployed}",
    )
    deployment_id = deployed["result"]["id"]
    observed = s3.json("cluster", "status", "--deployment-id", deployment_id)
    _expect(
        observed["lookup"] == deployed,
        f"status by id disagrees with apply: {observed}",
    )
    replayed = s3.json(
        "cluster", "apply", "--deployment-id", deployment_id, "--writers-stopped"
    )
    _expect(replayed == deployed, f"replay by id is not lookup-only: {replayed}")
    s3.unlock_retained("cluster apply")
    plan = s3.json("cluster", "plan", "--config", str(config))
    _expect(plan["changes"] == [], f"the S3 root did not converge: {plan}")

    query = s3.run(
        "--graph",
        GRAPH,
        "query",
        "everyone",
        "--query",
        str(config / "people.gq"),
        ok=False,
    )
    _expect(
        "local storage only" in query.stdout + query.stderr,
        f"query on the S3 root failed for another reason:\n{query.stdout}{query.stderr}",
    )
    print("  data commands still refuse an inline S3 root")

    refused = s3.run(
        "--as",
        ACTOR,
        "--graph",
        GRAPH,
        "cleanup",
        "--keep",
        "1",
        "--confirm",
        "--json",
        ok=False,
    )
    _expect(
        "refusing destructive `cleanup`" in refused.stdout + refused.stderr,
        f"cleanup without --yes failed for another reason:\n{refused.stdout}{refused.stderr}",
    )
    s3.assert_unlocked("after the refused cleanup")

    before = _object_count(s3_root)
    s3.json("--as", ACTOR, "--graph", GRAPH, "optimize")
    s3.unlock_retained("optimize")
    optimized = _object_count(s3_root)
    s3.json(
        "--as",
        ACTOR,
        "--graph",
        GRAPH,
        "cleanup",
        "--keep",
        "1",
        "--confirm",
        "--yes",
    )
    s3.unlock_retained("cleanup")
    cleaned = _object_count(s3_root)
    print(
        f"  objects: {before} before optimize, {optimized} after, {cleaned} after cleanup"
    )
    # The loads left one fragment per batch, so a real compaction writes a new
    # one, and cleanup --keep 1 then removes what the older versions held.
    _expect(optimized > before, "optimize wrote nothing to the S3 root")
    _expect(cleaned < optimized, "cleanup removed no objects from the S3 root")
    s3.assert_unlocked("after maintenance")

    back = work / "round-trip-root"
    _aws("s3", "cp", "--recursive", "--only-show-errors", s3_root, str(back))
    survived = _query_names(
        Omnigraph(binary, f"file://{back}", operator_config), config
    )
    _expect(
        survived == loaded,
        f"{len(loaded - survived)} loaded rows did not survive maintenance",
    )
    print(
        f"qualified: {len(loaded)} rows intact after optimize and cleanup on {s3_root}"
    )


@app.default
def qualify(binary: Path, bucket: str, prefix: str = "qualify-omnigraph-cli") -> None:
    """Run the qualification and exit non-zero on the first failure.

    :param binary: the patched omnigraph CLI.
    :param bucket: an existing S3 bucket the environment's credentials can write.
    :param prefix: key prefix under ``bucket``; each run adds a unique suffix.
    """
    binary = binary.resolve()
    s3_root = f"s3://{bucket}/{prefix}/{uuid.uuid4()}"
    with tempfile.TemporaryDirectory() as tmp:
        try:
            _run(binary, s3_root, Path(tmp))
        finally:
            _aws("s3", "rm", "--recursive", "--only-show-errors", s3_root)


if __name__ == "__main__":
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    app()
