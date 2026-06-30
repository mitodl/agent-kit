"""Tests for the schema/data migration commands."""

from .conftest import requires_omnigraph


@requires_omnigraph
def test_apply_schema_is_idempotent(server):
    from witan import server as srv

    # The test store was created with the current schema; re-applying is a no-op
    # that still succeeds and reports the store.
    res = srv.apply_schema()
    assert res["store"]
    assert srv._topic_schema_present() is True


@requires_omnigraph
def test_topic_schema_present_on_current_store(server):
    from witan import server as srv

    assert srv._topic_schema_present() is True
