"""Tests for policy/render_groups.py — the deploy-time Cedar membership render.

This runs on the omnigraph-server boot path with nothing downstream of it: if
it writes the wrong `groups:`, the cluster converges a policy that denies real
users and grants nobody, and the only symptom is a runtime denial naming an
actor that looks correctly provisioned. So the cases that matter here are the
mismatches — service ids that are not `act-` prefixed, groups with no
provisioned member, and drift between a bundle and this renderer.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

POLICY_DIR = Path(__file__).resolve().parent.parent / "policy"
_spec = importlib.util.spec_from_file_location(
    "render_groups", POLICY_DIR / "render_groups.py"
)
assert _spec and _spec.loader
render_groups = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_groups)

RenderError = render_groups.RenderError

# A realistic map: the two service accounts that are actually provisioned today
# (note: NOT `act-` prefixed) plus two humans.
LIVE_TOKEN_MAP = {
    "act-007fb23e": "t1",  # pragma: allowlist secret
    "act-11e9e82c": "t2",  # pragma: allowlist secret
    "svc-witan-ci": "t3",  # pragma: allowlist secret
    "svc-witan-admin": "t4",  # pragma: allowlist secret
}


def write_tokens(tmp_path: Path, mapping: dict[str, str]) -> Path:
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps(mapping))
    return path


def write_bundle(tmp_path: Path, name: str, groups: dict[str, list[str]]) -> Path:
    path = tmp_path / name
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "groups": groups,
                "rules": [
                    {
                        "id": "r",
                        "allow": {
                            "actors": {"group": next(iter(groups))},
                            "actions": ["read"],
                        },
                    }
                ],
            },
            sort_keys=False,
        )
    )
    return path


class TestClassifyActors:
    def test_service_ids_are_not_act_prefixed(self):
        """The mismatch that would otherwise dead-end CI and break-glass.

        The committed bundles name these `act-svc-witan-ci` etc.; the real
        token map does not. Classifying on the real keys is the whole point.
        """
        groups = render_groups.classify_actors(list(LIVE_TOKEN_MAP))
        assert groups["witan-ci"] == ["svc-witan-ci"]
        assert groups["witan-admin"] == ["svc-witan-admin"]

    def test_humans_land_in_witan_users(self):
        groups = render_groups.classify_actors(list(LIVE_TOKEN_MAP))
        assert groups["witan-users"] == ["act-007fb23e", "act-11e9e82c"]

    def test_unprovisioned_group_renders_empty_not_missing(self):
        """`svc-witan` is not provisioned anywhere yet — it must render `[]`.

        An empty group grants nobody, which is correct and safe. A *missing*
        group would make every rule referencing it fail to resolve.
        """
        groups = render_groups.classify_actors(list(LIVE_TOKEN_MAP))
        assert groups["witan-service"] == []

    def test_unknown_actor_is_an_error(self):
        """An id in no group could authenticate and do nothing — fail loudly."""
        with pytest.raises(RenderError, match="no Cedar group"):
            render_groups.classify_actors([*LIVE_TOKEN_MAP, "svc-something-new"])

    def test_membership_is_sorted(self):
        """Stable output keeps a restart from churning the applied bundle."""
        groups = render_groups.classify_actors(["act-zzz", "act-aaa"])
        assert groups["witan-users"] == ["act-aaa", "act-zzz"]


class TestLoadActorIds:
    def test_empty_map_is_an_error(self, tmp_path):
        """Applying a bundle against an empty map denies literally everyone."""
        with pytest.raises(RenderError, match="non-empty"):
            render_groups.load_actor_ids(write_tokens(tmp_path, {}))

    def test_missing_file_is_an_error(self, tmp_path):
        with pytest.raises(RenderError, match="cannot read"):
            render_groups.load_actor_ids(tmp_path / "absent.json")


class TestRenderBundle:
    def test_only_declared_groups_are_written(self, tmp_path):
        """The memory bundle declares no witan-ci; rendering must not add one."""
        bundle = write_bundle(
            tmp_path,
            "memory.policy.yaml",
            {"witan-users": ["act-alice"], "witan-admin": ["act-svc-witan-admin"]},
        )
        groups = render_groups.classify_actors(list(LIVE_TOKEN_MAP))
        written = render_groups.render_bundle(bundle, groups)

        assert written == ["witan-users", "witan-admin"]
        doc = yaml.safe_load(bundle.read_text())
        assert set(doc["groups"]) == {"witan-users", "witan-admin"}
        assert doc["groups"]["witan-admin"] == ["svc-witan-admin"]

    def test_fixture_ids_are_replaced(self, tmp_path):
        """The committed fixtures must not survive into the applied bundle."""
        bundle = write_bundle(
            tmp_path, "memory.policy.yaml", {"witan-users": ["act-alice", "act-bob"]}
        )
        render_groups.render_bundle(
            bundle, render_groups.classify_actors(list(LIVE_TOKEN_MAP))
        )

        doc = yaml.safe_load(bundle.read_text())
        assert "act-alice" not in doc["groups"]["witan-users"]
        assert doc["groups"]["witan-users"] == ["act-007fb23e", "act-11e9e82c"]

    def test_rules_are_preserved(self, tmp_path):
        """Only membership is templated — the rules are the tested deliverable."""
        bundle = write_bundle(tmp_path, "memory.policy.yaml", {"witan-users": ["x"]})
        before = yaml.safe_load(bundle.read_text())["rules"]
        render_groups.render_bundle(
            bundle, render_groups.classify_actors(list(LIVE_TOKEN_MAP))
        )
        assert yaml.safe_load(bundle.read_text())["rules"] == before

    def test_unknown_group_in_bundle_is_an_error(self, tmp_path):
        """Bundle/renderer drift must stop the boot, not render a silent empty."""
        bundle = write_bundle(tmp_path, "memory.policy.yaml", {"witan-typo": ["x"]})
        with pytest.raises(RenderError, match="drifted"):
            render_groups.render_bundle(
                bundle, render_groups.classify_actors(list(LIVE_TOKEN_MAP))
            )

    def test_non_bundle_file_is_an_error(self, tmp_path):
        path = tmp_path / "cluster.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "graphs": {}}))
        with pytest.raises(RenderError, match="not a policy bundle"):
            render_groups.render_bundle(path, {})

    def test_render_is_idempotent(self, tmp_path):
        """Every restart re-renders; the second pass must be a no-op."""
        bundle = write_bundle(tmp_path, "memory.policy.yaml", {"witan-users": ["a"]})
        groups = render_groups.classify_actors(list(LIVE_TOKEN_MAP))
        render_groups.render_bundle(bundle, groups)
        first = bundle.read_text()
        render_groups.render_bundle(bundle, groups)
        assert bundle.read_text() == first


class TestRealBundles:
    """Against the committed bundles, not synthetic ones."""

    @pytest.mark.parametrize(
        "name",
        [
            "memory.policy.yaml",
            "code-graph.policy.yaml",
            "bridge.policy.yaml",
            "server.policy.yaml",
        ],
    )
    def test_every_shipped_bundle_renders(self, tmp_path, name):
        """Each bundle baked into the image must survive the boot-path render.

        This is the drift guard: a group added to a bundle without a matching
        entry in SERVER_ACTOR_GROUPS/KNOWN_GROUPS fails here, in CI, rather
        than on a cluster where it denies its members.
        """
        bundle = tmp_path / name
        bundle.write_text((POLICY_DIR / name).read_text())
        groups = render_groups.classify_actors(list(LIVE_TOKEN_MAP))

        written = render_groups.render_bundle(bundle, groups)

        assert written, f"{name} declared no groups"
        doc = yaml.safe_load(bundle.read_text())
        for group in written:
            assert doc["groups"][group] == groups[group]

    def test_main_renders_all_bundles(self, tmp_path, capsys):
        for name in ("memory.policy.yaml", "server.policy.yaml"):
            (tmp_path / name).write_text((POLICY_DIR / name).read_text())
        tokens = write_tokens(tmp_path, LIVE_TOKEN_MAP)

        rc = render_groups.main(
            [
                "--tokens",
                str(tokens),
                str(tmp_path / "memory.policy.yaml"),
                str(tmp_path / "server.policy.yaml"),
            ]
        )

        assert rc == 0
        out = capsys.readouterr()
        assert "memory.policy.yaml" in out.out
        # witan-service is unprovisioned today; the warning is how that stays
        # visible rather than surfacing later as an unexplained denial.
        assert "witan-service" in out.err

    def test_main_fails_on_unknown_actor(self, tmp_path):
        (tmp_path / "memory.policy.yaml").write_text(
            (POLICY_DIR / "memory.policy.yaml").read_text()
        )
        tokens = write_tokens(tmp_path, {**LIVE_TOKEN_MAP, "mystery": "t"})

        rc = render_groups.main(
            ["--tokens", str(tokens), str(tmp_path / "memory.policy.yaml")]
        )

        assert rc == 1
