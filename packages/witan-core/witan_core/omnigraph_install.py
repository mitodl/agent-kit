"""The omnigraph binary installer, shared by both witan servers.

Neither server bundles the omnigraph binary at build time; ``witan setup`` /
``witan-code setup`` fetch the pinned release into ``~/.local/bin/`` at
install/runtime instead, so every install converges on the same version.

``_OMNIGRAPH_VERSION`` was previously duplicated verbatim in
``witan/setup.py`` and ``witan-code/setup.py`` and kept in lockstep by a
Renovate custom manager spanning both files. Now that it lives here once, the
custom manager targets this single file and the lockstep hack is gone.

``rich`` is imported lazily inside ``_download_omnigraph`` so merely importing
this module stays dependency-free; only actually running an install needs it
(both servers already depend on ``rich``).

THE OUTGOING BINARY IS KEPT, not overwritten into oblivion. omnigraph uses
strict single-version storage: a release that bumps the on-disk format makes
every store written by the old binary unopenable, and the only sanctioned
recovery (``witan migrate storage`` → :func:`witan.server.migrate_storage_format`)
has to *export with the old binary* first. An installer that replaced the
binary in place therefore deleted the one tool needed to rescue the data it had
just orphaned, and told the user to go find it again on GitHub. So an upgrade
sets the previous version aside as ``omnigraph-<version>`` beside ``dest``, and
:func:`preserved_binary` is how the migration path finds it.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

#: 0.10.0, from the real ``v0.10.0`` release — no longer the `edge` re-test.
#: 0.10.0 was reverted on 2026-08-14 for halving the write ceiling
#: (agent-kit#233); three witan-side confounds were fixed and the measurement
#: repeated on `edge` (tk-omnigraph-0-10-0-edge-halved-the-write-ceiling-r-7ba7c2).
#: Upstream cut ``v0.10.0`` on 2026-08-31T21:29Z, which ended the re-test by
#: giving the build under test an immutable tag: the paragraph that used to
#: stand here said the pin should go back to 0.9.0/v0.9.0 if the experiment had
#: concluded, and pinning the released 0.10.0 is that same instruction answered
#: forwards rather than backwards. The write-ceiling task holds the measurement.
_OMNIGRAPH_VERSION = "0.10.0"

#: WHICH UPSTREAM TAG THE BINARY IS FETCHED FROM. Normally ``v`` + the version
#: above; ``edge`` selects the rolling build of upstream ``main``, which
#: ``release-edge.yml`` force-updates and re-publishes on every push there.
#:
#: Kept as its own constant even now that it is just ``v`` + the version: the
#: two genuinely diverge on a moving tag, and collapsing them would break
#: either the URL or the "already installed, skipping" check, which compares
#: against what ``omnigraph --version`` actually prints.
#:
#: ★ OFF THE MOVING TAG AS OF 2026-08-31, AND THAT IS THE POINT OF THIS LINE.
#: ``edge`` is force-updated on every push to upstream main, so a pinned digest
#: describes a build that stops being downloadable the moment upstream merges:
#: it moved twice in the seven hours after the 2026-08-31 refresh (#305), and
#: each move turned `witan-code (code graph)` red on every open PR and left a
#: fresh ``witan setup`` unable to fetch a binary at all. A release tag is
#: immutable, so the digests below stay valid until someone deliberately moves
#: them.
#:
#: There is no flag or environment override for this — the tag is a property of
#: the repo, not of a run, precisely because all three tiers must agree on it
#: (``just check-omnigraph-pins``). To be certain which build you are on:
#: delete the binary and re-run ``witan setup``, which re-downloads and verifies
#: against the digest pinned below. To go BACK to a moving tag, set this to
#: ``edge`` and refresh those digests in the same commit — and expect the red
#: check described above to return with it.
#:
#: Renovate manages the VERSION line only (see renovate.json). Now that this is
#: a real ``v<version>``, a Renovate bump here IS meaningful — it must move this
#: tag, the three digests, and both Dockerfiles together, which is what
#: ``just check-omnigraph-pins`` enforces.
_OMNIGRAPH_RELEASE_TAG = "v0.10.0"

#: The on-disk storage format ``_OMNIGRAPH_VERSION`` is expected to read, as
#: reported by ``omnigraph version``'s ``internal-schema`` line. 0.8.x reads 4;
#: 0.9.x reads 6.
#:
#: THIS IS A DECLARATION, NOT A CACHE. Renovate bumps the version pin above and
#: cannot know about this line, so a release that moves the storage format
#: leaves the two disagreeing — which is exactly the signal
#: ``bin/check_omnigraph_format.py`` turns into a failing check. Editing this
#: number is how a human says "yes, I know this rebuilds every graph, and the
#: migration is planned".
#:
#: Do not update it to make CI green. Updating it is the last step of a format
#: migration, not the first: every local store and every deployed graph written
#: under the old number has to be rebuilt, and a 0.8.x binary refuses a 0.9.x
#: graph in both directions, so there is no gradual path and no downgrade.
_OMNIGRAPH_INTERNAL_SCHEMA = 6

_OMNIGRAPH_ASSETS: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "omnigraph-linux-x86_64.tar.gz",
    ("darwin", "arm64"): "omnigraph-macos-arm64.tar.gz",
}

#: SHA-256 of each asset, pinned in-repo. Keyed by asset NAME rather than by
#: platform so the two Dockerfiles — which select by ``TARGETARCH``, not by
#: ``platform.system()`` — can carry the identical values and
#: ``just check-omnigraph-pins`` can compare them across all three tiers.
#:
#: ★ THIS IS WHAT MAKES A MOVING TAG REPRODUCIBLE, AND IT IS NOT OPTIONAL WHILE
#: WE ARE ON ONE. ``edge`` is force-updated on every push to upstream main, so
#: the tag alone guarantees nothing: the installer and the two image builds can
#: each resolve it to a different commit and every version/tag check still
#: passes. A load-test result measured that way cannot be attributed to a build.
#: Downloading the published ``.sha256`` alongside the tarball does not fix that
#: either — it only attests to whichever build was current at download time.
#:
#: So the digest is recorded here, and a mismatch is a hard failure. When
#: upstream pushes, the next fetch FAILS LOUDLY rather than silently installing
#: a different binary; refreshing these values is then a deliberate act that
#: says "I am moving to a new build", exactly as editing
#: ``_OMNIGRAPH_INTERNAL_SCHEMA`` says "I know this rebuilds every graph".
#:
#: Refresh with, for each asset — note the published digest file drops the
#: `.tar.gz`, so it is `omnigraph-linux-x86_64.sha256`, not
#: `omnigraph-linux-x86_64.tar.gz.sha256` (that spelling 404s):
#:     curl -fsSL https://github.com/ModernRelay/omnigraph/releases/download/\
#: <tag>/<asset-without-.tar.gz>.sha256
#: and confirm it against the tarball you actually downloaded (`sha256sum`) in
#: the same sitting — on a moving tag the two assets can be republished a
#: minute apart, and a digest read across that gap describes neither build.
#: ★ THE DIGESTS BELOW ARE THE RELEASED `v0.10.0` (2026-08-31T21:29Z), NOT an
#: `edge` build and not v0.9.0's. THAT IS THE ONLY LINE HERE STATING WHICH
#: BUILD IS PINNED — everything after it is the refresh history in order,
#: appended rather than rewritten, and each block describes the build CURRENT
#: AT ITS OWN DATE. Read the last block for what is pinned now; read the
#: earlier ones for what was checked and what it cost to learn.
#:
#: The "confirm it against the tarball you actually downloaded in the same
#: sitting" instruction above was written for the moving tag and is now belt
#: and braces rather than load-bearing: a release tag cannot be republished
#: under you mid-refresh. It was still done for this one.
#:
#: ── 2026-08-24, the `edge` build through bb0e3dc8bf ──
#: Refreshed from the 2026-08-24T12:50Z triple (972f1666c5) after CI failed
#: the checksum check on agent-kit#283 — the SECOND refresh in one day, which
#: is the moving tag behaving as documented below rather than anything going
#: wrong.
#:
#: Two commits landed in between and only ONE carries behaviour:
#:   #545 `feat(azure): implement RFC-0029 Blob storage preview` — 82 files,
#:        +8904/-465. A new `omnigraph-azure-admission` crate and Container
#:        Apps reference, plus the scheme-dispatch refactor it needed.
#:   #547 `docs: rebuild guides and normalize RFC corpus` — 111 markdown
#:        files and 8 `.rs`, but every one of those Rust hunks is a doc
#:        comment: RFC renumbering (`RFC-010` -> `RFC 0010`) and doc-path
#:        fixes in identity.rs/planes.rs/table_store.rs/exec/query.rs. Read
#:        line by line, not inferred from the subject.
#:
#: ★ #545 IS A MUCH LARGER SURFACE THAN THE LAST REFRESH AND IT DOES TOUCH
#: WITAN'S PATHS — cluster config/serve/store/sweep, the CLI, server settings,
#: manifest, table_store. It is NOT confined to a feature witan ignores the
#: way #522's change feed was. The three things that make it safe anyway were
#: each checked rather than assumed:
#:   1. s3 root parsing was refactored out of an inline `strip_prefix("s3://")`
#:      into `omnigraph_storage::normalize_root_uri`, but for `StorageKind::S3`
#:      that function returns `trim_trailing_slashes(uri)` — literally the old
#:      `trim_end_matches('/')`, plus an empty-guard. `s3://` still classifies
#:      as S3 in `storage_kind_for_uri`. A deployed root like
#:      `s3://ol-data-witan-production/fmt6/...` normalises identically.
#:   2. The container entrypoint gained an Azure admission wrapper, but it is
#:      gated on the scheme: `case "$cluster_root" in az://*) ...wrapper... ;;
#:      *) exec "$SERVER_BIN" "$@" ;;`. An s3 root takes the unchanged branch.
#:   3. Azure is explicitly NOT production-supported in this release (the
#:      v0.10.0 notes say so), so nothing here is a path we can reach.
#:
#: The tarball now ships THREE binaries — `omnigraph`, `omnigraph-server` and
#: the new `omnigraph-azure-admission` — where it shipped two. Inert for us,
#: but it is why the assets grew.
#:
#: Checked the two things every refresh here checks — the `_RETRYABLE` /
#: `_NEEDS_REPAIR` / `_PRECONDITION_FAILED` substrings in omnigraph.py and the
#: `"storage: "` prose prefix witan's classifier keys on — and found no
#: rename. Counted tree-wide at BOTH refs rather than only reading the diff:
#: 9 of the 11 substrings are non-zero and identical, and `precondition
#: failed` grew by 2, both of those being HTTP 412 fixtures in the new Azure
#: crate's tests. The `"storage: "` hits in the diff are cluster.yaml CONFIG
#: keys (`storage: "az://..."`), not the prose error prefix — same literal,
#: different thing. Confirmed against the artifact with `strings` too.
#:
#: ★ A TRAP IN SCANNING THAT DIFF, AND IT COST A WRONG ANSWER ONCE. GitHub's
#: commit API omits `patch` for a file too large to inline, and #545's
#: `crates/omnigraph-storage/src/lib.rs` (+2476/-45) is exactly that file —
#: the single likeliest home for the error vocabulary. A diff scan therefore
#: reported "no vocabulary change" while being structurally blind to the one
#: file that mattered. Fetching both versions of that file and grepping THEM
#: then returned all-zeros for 10 of 11 substrings, which reads like a clean
#: result and is worse: those strings do not live in that file at all. Only a
#: tree-wide `git grep` at both refs is an instrument that can see the data.
#: Check that a scan returns a NON-zero count somewhere before believing a
#: zero anywhere.
#:
#: ★ AND A TRAP IN THE `strings` CONFIRMATION, WORTH KNOWING BEFORE RE-RUNNING
#: IT. `manifest table version` and `ahead of manifest` do NOT appear in the
#: binary — and did not in the previous build either. They are assembled at
#: runtime from fragments, so their absence from `strings` says nothing about
#: this refresh. Re-confirmed on THIS build: the same 9 present, the same 2
#: absent. Check a suspicious absence against the OLD binary before reading it
#: as a regression; two of the eleven substrings look alarming and always have.
#:
#: ★ AND `edge` MOVED AGAIN WHILE THIS REFRESH WAS BEING PREPARED. The first
#: attempt captured the digests of the 17:24-17:35Z build (0f1a50d0be) — but
#: the `edge` TAG had already advanced to bb0e3dc8bf and its Release Edge run
#: was still building, so those digests would have been stale within the hour.
#: Read `refs/tags/edge` directly (`git ls-remote`, or the git-ref API) rather
#: than inferring the build from asset timestamps, and check whether a Release
#: Edge run is in flight before capturing anything. Note also that
#: `release-edge.yml` has `paths-ignore` for `**/*.md` — but a "docs" commit
#: that also touches one `.rs` file, as #547 did, still rebuilds.
#: That is the cost of the moving tag, not a mishap: upstream merges several
#: times a day and each push republishes `edge`, so a digest here can be stale
#: before CI runs. Expect to refresh this on a red witan-code job rather than
#: on a schedule, and prefer a real `v<version>` tag the moment 0.10.x has one
#: (there is no v0.10.0 release yet, which is the only reason this is still on
#: `edge` — `docs/releases/v0.10.0.md` exists upstream but is marked
#: unreleased).
#:
#: The digests below were taken by downloading all three tarballs and hashing
#: them locally, then cross-checking each against the release's published
#: `.sha256` in the same sitting — all three matched. Upstream head, the `edge`
#: ref, and all three asset timestamps+sizes were read before AND after the
#: downloads and were identical (head/edge bb0e3dc8bf, assets 19:47-19:58Z),
#: so this triple describes one build rather than a window. Still the 0.10.0
#: re-test (tk-omnigraph-0-10-0-edge-halved-the-write-ceiling-r-7ba7c2);
#: version reports 0.10.0 and internal-schema 6, both read off THIS binary via
#: `bin/check_omnigraph_format.py` ("omnigraph 0.10.0 reads storage format 6,
#: as declared."), so this is not a rebuild-every-graph event — the v0.10.0
#: notes independently confirm the manifest schema stays at v6.
#:
#: ★ REFRESHED AGAIN 2026-08-26, after `witan-code (code graph)` went red on
#: agent-kit#289 with the by-now-familiar checksum mismatch. `edge` had moved
#: from bb0e3dc8bf to f714e5961147, two commits later:
#:   #551 `feat(engine): expose branch-merge table-walk timing` — confined to
#:        `crates/omnigraph/src/exec/merge.rs` and `instrumentation.rs`, plus
#:        tests/docs. Opt-in developer instrumentation
#:        (`MergeWriteProbes::merge_timing_snapshot`); the release notes say
#:        outright "Production leaves the task-local probe unset and performs
#:        no timing clock reads," and separately reconfirm "Internal manifest
#:        schema remains v6."
#:   #553 `ci: move Azure and vocabulary audits off PRs` — workflow files
#:        only (`.github/workflows/*`, a new gating script). Ships nothing in
#:        the binary.
#: Re-ran the same vocabulary check as every prior refresh — a tree-wide `git
#: grep` for every `_RETRYABLE`/`_NEEDS_REPAIR`/`_PRECONDITION_FAILED`/
#: `_RECOVERY_REQUIRED` substring and the `"storage: "` prefix, at both refs —
#: and every match count came back identical; no rename, no removal.
#: `bin/check_omnigraph_format.py` against the freshly-installed binary again
#: read "omnigraph 0.10.0 reads storage format 6, as declared."
#:
#: Each digest below was independently confirmed twice: against the release's
#: published `.sha256`, and against a tarball downloaded fresh and hashed
#: locally, in the same sitting. Worth naming why that second check matters —
#: the first `omnigraph-linux-arm64.tar.gz` download here truncated
#: mid-transfer (a plain connection reset, `curl` exit 56) and hashed to a
#: THIRD value, distinct from both the published digest and the one below; a
#: retry matched. A truncated download can produce a stable, wrong hash
#: rather than an obvious error — check the transfer actually completed
#: before trusting a locally-computed digest, not just that `curl` printed
#: something.
#:
#: ★★ REFRESHED AGAIN 2026-08-31, and THIS ONE IS NOT LIKE THE OTHERS. Red
#: `witan-code (code graph)` again (agent-kit#298, and every other open PR),
#: but `edge` had moved f714e5961147 -> ac620eea87e8a91cc5349276c3afc58f40fe4308:
#: 29 commits, 160 files. Every previous refresh here was two or three commits.
#: Read the six that reach witan rather than the subject lines:
#:
#:   #561 `fix(recovery): heal stranded effect-free intents without a reopen`
#:        ★ THIS CLOSES OUR OWN UPSTREAM ISSUE #554 — the pending-Mutation
#:        recovery barrier that wedged the production code-bridge graph for
#:        ~15 hours (tk-production-code-bridge-graph-is-wedged-on-a-pend-8318a4,
#:        whose only remaining item was "upstream response on #554; that is
#:        the real fix"). A write dying between arming its recovery sidecar
#:        and its first table commit stranded an Armed, effect-free intent,
#:        and the heal deferred every Armed intent to the next ReadWrite open
#:        — which a long-lived server never performs, so the barrier refused
#:        every write until a restart. Now re-proven effect-free from the live
#:        heal and retired. ★ DO NOT BUILD THE ROLLOUT-RESTART STOPGAP that
#:        task holds in reserve; this is the actual fix.
#:
#:   #569 `fix(query): fail closed on missing required parameters`
#:        ★ THE ONE BEHAVIOUR CHANGE THAT COULD BREAK US, and it is a change
#:        in our favour. Previously an unresolved parameter in a node-property
#:        match caused the pushdown filter to be OMITTED — silently widening
#:        the query to every row. Now it errors before scanning. So any read
#:        that ever omitted a declared parameter was quietly returning
#:        over-broad results and will now fail loudly instead.
#:        Note `omnigraph.py`'s read path does NOT validate that every
#:        declared parameter is supplied, the way `change`/`_compose_steps`
#:        do — it just `json.dumps(params)`. Nothing in 2172 tests trips this
#:        (see below), so no such caller exists today, but the read path has
#:        no guard keeping it that way.
#:
#:   #579 `fix(branch): name every branch life by an incarnation-suffixed
#:        native ref` — deleting a branch and recreating it under the SAME
#:        NAME reused the same storage paths, so a warm handle's path-keyed
#:        cache served the dead life's metadata to the new one ("all columns
#:        in a record batch must have the same length", or stale rows).
#:        witan-code does exactly that: `reaper` deletes a stale branch view
#:        and `GraphStore.ensure_branch` recreates it under the git branch's
#:        name. Latent bug we had not hit yet, now fixed upstream.
#:
#:   #531 `perf(mutate): stage independent tables at the loader's write
#:        concurrency` — `stage_all` now stages at `OMNIGRAPH_LOAD_CONCURRENCY`
#:        (default 8) instead of a pinned 1. ★ THIS INVALIDATES EVERY
#:        WRITE-CEILING NUMBER THIS PROJECT HOLDS: they were all measured on
#:        pinned-1 staging. Re-measure before trusting
#:        tk-the-write-gate-is-sized-against-a-3-45s-solo-wri-73fc2b's EWMA
#:        sizing or tk-omnigraph-0-10-0-edge-halved-the-write-ceiling-r-7ba7c2.
#:        Publication, ordering and failure semantics are untouched upstream.
#:
#:   #542 `perf(engine): reclaim deleted branch forks in the background` —
#:        `branch_delete` now returns at ref removal, with the per-dataset
#:        Lance fork reclaim continuing asynchronously. The reaper's deletes
#:        get faster; anything that deletes and then measures storage, or
#:        immediately recreates, is now racing physical cleanup. Safe in
#:        combination with #579, which stops a recreated branch sharing paths
#:        with its predecessor.
#:
#:   #571 `perf(query): enable projection pushdown for node scans` — a node
#:        scan read every column of every row regardless of what the query
#:        referenced; the projection came from the schema alone and RETURN was
#:        applied in memory afterwards. Now derived from the query.
#:        ★ THE HEADLINE UPSTREAM NUMBER DOES NOT APPLY TO US, and it is worth
#:        saying so rather than quoting it: their ~13 GB/OOM case is a
#:        never-referenced `Vector(3072)` column dominating the read, and
#:        witan defaults `WITAN_EMBED_ENABLED` OFF — `recall` runs BM25-only
#:        and needs no embedding provider, so our Memory rows carry no vector
#:        to skip. What we get is the narrower win of not reading unreferenced
#:        scalar columns. The big version of this only arrives if embeddings
#:        are ever switched on, at which point re-read this.
#:
#: The rest is benchmark/DST harness work (#527, #555-#560, #568/#570/#577),
#: merge-lineage internals (#540/#541, #573), CI, and docs.
#:
#: CHECKS RUN, same as every refresh here plus one this jump earned:
#:   * Vocabulary — tree-wide `git grep` at BOTH refs (never a diff scan; see
#:     the trap recorded above) for all 13 `_RETRYABLE`/`_NEEDS_REPAIR`/
#:     `_PRECONDITION_FAILED`/`_RECOVERY_REQUIRED` substrings. ALL 13
#:     IDENTICAL. `storage:` moved 66 -> 80 files, and every one of the 14 is
#:     in the NEW `omnigraph-bench`/`omnigraph-dst` crates; none is in a
#:     shipping crate, and the tarball still contains exactly `omnigraph`,
#:     `omnigraph-server`, `omnigraph-azure-admission`. Zero files lost a hit.
#:     The instrument returned non-zero for 13 of 14 rows, so a zero here is
#:     readable as data rather than as a broken scan.
#:   * Version/format — `omnigraph version` on THIS binary reads 0.10.0 and
#:     internal-schema 6, unchanged. NOT a rebuild-every-graph event.
#:   * ★ THE WHOLE TEST SUITE AGAINST THIS BINARY, which no previous refresh
#:     here did and a 29-commit jump with a query-semantics change warrants:
#:     witan-core 584, witan-council 1008, witan-code 580 — 2172 passed. (One
#:     failure, `test_pre_upgrade_candidates_exclude_the_current_binary`, was
#:     an artifact of putting a second `omnigraph` on PATH to run this at all;
#:     it passes without the override, and the function under test is pure
#:     PATH-scanning Python that never invokes the binary.)
#:
#: Digests taken by downloading all three tarballs and hashing them locally,
#: cross-checked against each published `.sha256` in the same sitting — all
#: three matched. `refs/tags/edge` read directly before AND after every
#: download and identical throughout (ac620eea87), so this triple describes
#: one build rather than a window.
#:
#: Reverting the experiment means restoring the v0.9.0 triple, which was:
#:     linux-x86_64  507a36f385bea073e7f284fe476befbb4cd788b32bfa85d6f4cd5e943b663197
#:     linux-arm64   6742a7fcf2761cb5841a38990c38383d7a884da2c65e3e7cc884afbbf2b2d881
#:     macos-arm64   69f78c93e661e8ea2b92deafe6330650a0921a003c2099b75b226482a90dc03e
#:
#: ★★ 2026-08-31, THE SECOND ENTRY OF THIS DATE, AND IT LEAVES THE MOVING TAG.
#: Not a refresh: `_OMNIGRAPH_RELEASE_TAG` goes `edge` -> `v0.10.0`.
#:
#: WHY NOW — THE PIN BROKE TWICE IN ONE DAY, and the second time is this entry.
#: `witan-code (code graph)` is uncached precisely to report this, so its
#: history dates the breakage:
#:   * 11:32Z — red on agent-kit#300 and #302, against the PRE-#305 pin.
#:   * 14:44Z — #305 refreshes to ac620eea87. Green again at 14:45-14:53Z
#:     (#298, #138, #304), and still green at 16:36Z (#306) and 18:35Z (#307).
#:   * Four commits then land upstream (18:28Z, 19:05Z, 19:36Z, 20:16Z), each
#:     force-updating `edge` and republishing its assets.
#:   * 20:13Z — red again on #308 and #309, and 21:21Z on #310. PRs still
#:     showing green are holding results from before the move, not passing now.
#: So the refresh above held roughly six hours. A fresh `witan setup` fails the
#: same way and for the same reason: the pinned digest names a tarball upstream
#: has already replaced. The block above spent its length on which 29 commits
#: arrived; the recurring cost was never the reading, it was the half-life.
#:
#: Upstream cut `v0.10.0` at 21:29Z tagging
#: a625748c8bf41e21654c48321fa31d295add7621 — EXACTLY the commit the
#: then-current `edge` build was cut from (`compare` reports identical) — so
#: this pins the same source under a name that cannot move. The re-test the
#: header paragraph described is over by virtue of its subject shipping.
#:
#: WHAT ARRIVED since the pinned ac620eea87: 4 commits, 67 files.
#:   #581 `feat(storage): upgrade to Lance 11 with safe full-text rebuilding`
#:        — the only one carrying operator consequence. See the ★ below.
#:   #582 `fix(compiler): reject undeclared variables in property matches`
#:        A `$var` used in a match property must now be a declared query
#:        parameter, or typecheck fails with T3. This is about QUERY TEXT, not
#:        about the params dict a caller passes: an extra key the query does not
#:        declare is still accepted and ignored. `queries/*.gq` are clean under
#:        it — the suites below exercise every one of them.
#:   #585 `release: qualify v0.10 upgrade and isolate FTS test counters`
#:        Upstream's own cross-version v0.9->v0.10 suite plus release notes.
#:   #586 `test(bench): remove synthetic worker timing race` — tests only.
#:
#: ★ LANCE 11 CHANGES THE ANALYZER, AND FULL-TEXT SEARCH FAILS CLOSED UNTIL
#: EACH BRANCH IS REBUILT. `_OMNIGRAPH_INTERNAL_SCHEMA` stays 6 and `omnigraph
#: version` on this binary agrees, so `bin/check_omnigraph_format.py` is green
#: — correctly, because the on-disk format did not move. What moved is the
#: full-text analyzer.
#:
#: The silent-under-return that motivated upstream #581 (their regression cites
#: `organism` and `university`) is what RAW Lance does on a generation
#: mismatch. 0.10.0 does NOT ship that behaviour: it added a guard. A selected
#: index whose analyzer generation cannot be proven compatible raises
#: `OmniError::FullTextIndexRebuildRequired` — HTTP 409 with a
#: `full_text_index_rebuild_required` detail, and upstream's own doc comment
#: says "Ordinary reads remain available; do not return a partial indexed
#: result". So search is UNAVAILABLE, not quietly worse, and non-search reads
#: are untouched.
#:
#: Every `search()`/`bm25()` query in `read.gq` sits on such an index — memory
#: search and the Task/WorkflowProject BM25 search both — so on any graph
#: written before this binary, those queries are refused until:
#:     omnigraph rebuild-full-text-indexes <URI> --branch <branch>
#: Upstream calls it a controlled cutover, not a rolling upgrade: stop old
#: readers/writers and keep a recoverable backup first. A local store is one
#: command; the DEPLOYED graph needs scheduling, which is
#: tk-rebuild-full-text-indexes-on-the-deployed-witan--076eb6. Nothing here
#: performs it — this constant only decides which binary a future install or
#: image build fetches.
#:
#: ★ AND THE CLIENT HAD TO LEARN THAT 409 FIRST, which is why this commit is
#: not digests alone. `classify_status` treats a bare 409 as RETRYABLE on the
#: status, so an un-taught client would retry every refused search the full
#: budget and then report a timeout-shaped failure, burying the remedy the
#: server already printed. `_http.FULL_TEXT_REBUILD_REQUIRED` classifies it
#: terminal on both transports.
#:
#: CHECKS RUN, against the v0.10.0 binary (downloaded, digest-verified, run
#: from a scratch dir):
#:   * Vocabulary — all 14 `_RETRYABLE`/`_NEEDS_REPAIR`/`_PRECONDITION_FAILED`/
#:     `_RECOVERY_REQUIRED` substrings against the 4-commit range. Every PROSE
#:     marker (`stale view`, `omnigraph repair`, `refresh and retry`,
#:     `reprepare from the current branch`, `write authority`, `ahead of
#:     manifest`, ...) is untouched. `recovery_required`/`precondition_failure`
#:     appear on both sides of `omnigraph-server/src/lib.rs` (+149/-271), a
#:     refactor that preserves the HTTP field names rather than renaming them.
#:   * Version/format — reads 0.10.0, internal-schema 6, unchanged.
#:   * Suites against this binary: witan-core 588, witan-council 1009,
#:     witan-code 580 — 2177 passed, NO failures and no artifact exclusions.
#:
#:     ★ GETTING THAT NUMBER HONESTLY TAKES ONE STEP, and skipping it silently
#:     tests the wrong binary. `testsupport/hermetic.py` PREPENDS the real
#:     `~/.local/bin` to PATH (deliberately — see its docstring), so putting a
#:     candidate binary earlier on PATH does NOT make the suite use it: the
#:     machine's installed omnigraph still wins, and everything passes while
#:     proving nothing about the new one. Run with `HOME` pointed at a scratch
#:     dir holding the candidate at `$HOME/.local/bin/omnigraph`, with the real
#:     `~/.local/bin` OFF PATH so only one omnigraph is reachable — otherwise
#:     `test_pre_upgrade_candidates_exclude_the_current_binary` correctly
#:     reports the second one and looks like a failure.
#:
#: Digests taken by downloading all three tarballs and hashing them locally,
#: cross-checked against each published `.sha256` — all three matched. On an
#: immutable tag the same-sitting caveat no longer bites.
#:
#: Going back to the moving tag means restoring the `edge` triple superseded
#: here, which was the ac620eea87 build:
#:     linux-x86_64  6a0fba8842a2071c558abf2c1a399ce5e11d359dff78b6ae6ff3676617f95680
#:     linux-arm64   dd40fa4169a89af41cddbdeb8fe441b714438633297e153876b4889ec0af3a86
#:     macos-arm64   990fcab686922f885f959a0f6204f61d0770ef7af6f058bac9df14cc587a2248
_OMNIGRAPH_ASSET_SHA256: dict[str, str] = {
    "omnigraph-linux-x86_64.tar.gz": (
        "05d3ce4ec0ab51a876befd89b643c3e7f2d5489be0398a38cef6fb3a0d257fc1"
    ),
    "omnigraph-linux-arm64.tar.gz": (
        "dd3ac09123a68882454db7e689da4c306c41677826237098df4e76b0f73d8d5e"
    ),
    "omnigraph-macos-arm64.tar.gz": (
        "7c3b8fadbe590486a192c734d8c3d38cce0e4da1f02940e6ac306c1ada67f171"
    ),
}
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
#: Anchored, and a full semver — so the sweep that prunes stale set-aside
#: binaries can never match something a user put on their own PATH by hand
#: (``omnigraph-dev``, ``omnigraph-patched``). Only what this module wrote.
_PRESERVED_RE = re.compile(r"^omnigraph-(\d+\.\d+\.\d+)$")


def _installed_version(dest: Path) -> str | None:
    """Return ``dest``'s reported version, or ``None`` if absent/unreadable.

    A hung, corrupted, or non-executable binary must degrade to "unknown
    version" (triggering a re-download) rather than crash `setup` —
    ``subprocess.TimeoutExpired`` is a ``SubprocessError``, not an
    ``OSError``, so both need catching, and a non-zero exit means the
    output isn't trustworthy version text even if something printed.
    """
    if not dest.exists():
        return None
    try:
        result = subprocess.run(
            [str(dest), "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = _VERSION_RE.search(result.stdout + result.stderr)
    return match.group(0) if match else None


def default_install_path() -> Path:
    """Where :func:`install_omnigraph` puts the binary."""
    return Path.home() / ".local" / "bin" / "omnigraph"


def reported_internal_schema(binary: str | Path = "omnigraph") -> int:
    """The on-disk storage format ``binary`` reads, per ``omnigraph version``.

    The number that decides whether an upgrade is a rebuild-everything event.
    Read from the binary rather than inferred from its release number, because
    the mapping is upstream's to change and has no published table.

    Raises ``RuntimeError`` rather than returning a sentinel: every caller is
    asking in order to compare against a declared value, and a comparison
    against "unknown" that quietly passes is the failure mode this whole
    mechanism exists to prevent.
    """
    try:
        result = subprocess.run(
            [str(binary), "version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not run `{binary} version`: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"`{binary} version` failed ({result.returncode}):\n{result.stderr}"
        )
    for line in (result.stdout + result.stderr).splitlines():
        if line.strip().startswith("internal-schema"):
            return int(line.split()[-1])
    raise RuntimeError(
        f"`{binary} version` reported no internal-schema line:\n{result.stdout}\n"
        "The storage-format checks depend on it; upstream may have renamed or "
        "dropped it."
    )


def preserved_binaries(dest: Path | None = None) -> list[Path]:
    """Every pre-upgrade binary this installer set aside, newest version first.

    Named ``omnigraph-<version>`` beside ``dest`` by :func:`_preserve_outgoing`.

    ALL OF THEM, NOT JUST THE NEWEST, and the caller is expected to try each in
    turn. There is no single "the previous binary", because there is no single
    store: witan keeps one memory graph, but witan-code keeps a separate
    ``<slug>.omni`` per repository (``witan_code.config.Config.code_dir``), and
    those are only ever migrated when someone next opens that repo. Cross two
    format versions while a repo sits untouched and its store is two releases
    behind — older than the newest set-aside binary, and readable only by one
    further back.

    Ordering is by parsed version rather than filename, so ``0.10.0`` sorts
    above ``0.9.0`` instead of below it.
    """
    target = dest or default_install_path()
    found: list[tuple[tuple[int, ...], Path]] = []
    for entry in target.parent.glob("omnigraph-*"):
        match = _PRESERVED_RE.match(entry.name)
        if match and entry.is_file() and os.access(entry, os.X_OK):
            parsed = tuple(int(part) for part in match.group(1).split("."))
            found.append((parsed, entry))
    return [path for _, path in sorted(found, reverse=True)]


class OmnigraphInstallFailed(RuntimeError):
    """The installer declined to put a binary in place, and said why.

    ★ RAISED SO THE STEP THAT ASKED FOR THE INSTALL IS THE STEP THAT FAILS.
    Every path below used to print its reason and return, so a workflow step
    running `install_omnigraph(dry_run=False)` exited 0 with the refusal buried
    in its log. The refusal then resurfaced ten tests later as `RuntimeError:
    omnigraph binary not found. Install via: witan-code setup` — which reads as
    a broken test environment rather than as a supply-chain check doing exactly
    its job, and cost real time to trace on 2026-08-20.

    A moved `edge` tag is the common cause and the one worth naming: the digest
    check catching it is the system working, and it should look like it.
    """


def install_omnigraph(dry_run: bool = False, *, strict: bool = True) -> None:
    """Fetch the pinned omnigraph release into ``~/.local/bin/``.

    Skips the download when a binary is already present and reports the
    pinned version via ``--version``, so re-running always converges on the
    current pin without refetching an already-correct binary.

    ``strict`` (the default) raises :class:`OmnigraphInstallFailed` when no
    binary ends up installed. Pass ``strict=False`` to keep the old
    print-and-return behaviour.

    ★ THE DEFAULT IS THE STRICT ONE ON PURPOSE. The callers that most need the
    failure are the seven workflow steps invoking this through `python -c`, and
    they cannot pass an argument without being edited — so the default has to
    be the loud one or they keep swallowing it. The two callers that legitimately
    want to continue are `witan setup` and `witan code setup`, which are
    interactive, ask for several unrelated things in one run, and would
    otherwise abort before writing config.toml and the agent bundles over a
    binary the user can install separately. Those two opt out explicitly.

    NOT AN ERROR EITHER WAY: an unsupported platform, and a binary already at
    the pinned version. Neither is a failure to install — the first is a
    platform this installer does not build for (witan works fine with an
    omnigraph put on PATH by other means), and the second is the converged
    state re-running is supposed to reach.
    """
    _download_omnigraph(default_install_path(), dry_run, strict=strict)


def _preserve_outgoing(dest: Path, version: str | None, console) -> None:
    """Set the outgoing binary aside as ``omnigraph-<version>`` beside ``dest``.

    Copied rather than moved: the copy runs *before* the atomic replace, and a
    move would leave the user with no working ``omnigraph`` at all in the
    window between the two, or permanently if the replace then failed.

    ★ EVERY PREVIOUS VERSION IS KEPT. Nothing is pruned here, and that is
    deliberate — an earlier revision of this function swept all but the newest,
    on the reasoning that a store has exactly one writer. True per store, and
    irrelevant: there are many stores. witan-code keeps one ``<slug>.omni`` per
    repository, each migrated only when someone next opens that repo. Upgrade
    across two format versions while a repo lies untouched and its store is two
    releases behind — so the sweep would delete the only binary able to export
    it, permanently, with no warning and no way back.

    The cost of not pruning is disk (these binaries are ~220 MB each) bounded
    by how many format versions a machine traverses, which is small. The cost
    of pruning is unrecoverable data. Retiring old copies is safe only once
    every store is known migrated, which this function cannot know and should
    not guess.

    Best-effort: failing to set the old binary aside must not abort an
    otherwise-working upgrade, so an ``OSError`` here warns and returns rather
    than raising. The user is left exactly where they were before this
    function existed, which is survivable; a failed install is not.
    """
    if not version or version == _OMNIGRAPH_VERSION or not dest.is_file():
        return
    keep = dest.with_name(f"omnigraph-{version}")
    try:
        shutil.copy2(dest, keep)
        keep.chmod(0o755)
    except OSError as exc:
        console.print(
            f"  [yellow]omnigraph[/yellow] — could not set v{version} aside "
            f"({exc}); `witan migrate storage` will need it passed by hand"
        )
        return
    console.print(f"  [dim]omnigraph[/dim] — previous v{version} kept at {keep}")


def _download_omnigraph(dest: Path, dry_run: bool, *, strict: bool = True) -> None:
    try:
        from rich.console import Console
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "the omnigraph installer needs `rich` for its progress output; "
            "install it via the witan-core[cli] extra (both servers already "
            "depend on rich, so this only bites a bare witan-core install)."
        ) from exc

    console = Console()

    def refuse(markup: str, plain: str) -> None:
        """Print the reason as before, then raise it unless the caller opted out.

        Two texts rather than one: the console gets Rich markup, and the
        exception must not — a `[red]` in an exception message is noise in a
        traceback and, worse, is swallowed whole by anything that renders it
        through Rich (`witan setup`'s own console does).
        """
        console.print(markup)
        if strict:
            raise OmnigraphInstallFailed(plain)

    # Read once and carry it: this is both the skip check and, further down,
    # the name the outgoing binary is set aside under. Re-reading after the
    # download would be reading the *new* binary.
    installed = _installed_version(dest)
    if installed == _OMNIGRAPH_VERSION:
        console.print(
            f"  [dim]omnigraph[/dim] — {dest} already at v{_OMNIGRAPH_VERSION}, skipping"
        )
        return

    key = (platform.system().lower(), platform.machine().lower())
    asset = _OMNIGRAPH_ASSETS.get(key)
    if asset is None:
        console.print(
            f"  [yellow]omnigraph[/yellow] — no pre-built binary for"
            f" {key[0]}/{key[1]}; install manually"
        )
        return

    url = (
        f"https://github.com/ModernRelay/omnigraph/releases/download"
        f"/{_OMNIGRAPH_RELEASE_TAG}/{asset}"
    )
    console.print(
        f"  downloading omnigraph {_OMNIGRAPH_RELEASE_TAG} "
        f"(expected v{_OMNIGRAPH_VERSION}) …"
    )

    if dry_run:
        console.print(f"  [green]omnigraph[/green] → {dest} [dim](dry-run)[/dim]")
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_name(dest.name + ".tmp")
    try:
        extracted = False
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / asset
            try:
                with (
                    urllib.request.urlopen(url, timeout=60) as resp,
                    open(archive, "wb") as fh,
                ):
                    fh.write(resp.read())
            except Exception as exc:  # noqa: BLE001
                refuse(
                    f"  [red]omnigraph download failed[/red] ({exc}); install manually",
                    f"omnigraph download failed: {exc}",
                )
                return
            # ★ VERIFY BEFORE EXTRACTING, and refuse rather than warn. Nothing
            # checked these bytes before this: the installer put whatever the
            # URL returned onto a developer's PATH. On a moving tag it is also
            # the only thing tying the binary to the build this repo was tested
            # against — see _OMNIGRAPH_ASSET_SHA256.
            expected = _OMNIGRAPH_ASSET_SHA256.get(asset)
            if expected is None:
                refuse(
                    f"  [red]omnigraph[/red] — no pinned checksum for {asset}; "
                    "refusing to install an unverified binary",
                    f"no pinned checksum for {asset}; refusing to install an "
                    "unverified binary. Add its digest to "
                    "_OMNIGRAPH_ASSET_SHA256.",
                )
                return
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            if actual != expected:
                refuse(
                    f"  [red]omnigraph checksum mismatch[/red] for {asset}\n"
                    f"    expected {expected}\n    got      {actual}\n"
                    f"  The '{_OMNIGRAPH_RELEASE_TAG}' tag has moved, or the "
                    "download was corrupted. Refresh the pinned digest "
                    "deliberately — do not install this.",
                    f"omnigraph checksum mismatch for {asset}: expected "
                    f"{expected}, got {actual}. The "
                    f"'{_OMNIGRAPH_RELEASE_TAG}' tag has moved, or the download "
                    "was corrupted. Refresh the pinned digest in "
                    "witan_core/omnigraph_install.py deliberately — do not "
                    "install this.",
                )
                return
            with tarfile.open(archive) as tf:
                for member in tf.getmembers():
                    if member.name.split("/")[-1] == "omnigraph" and not member.isdir():
                        f = tf.extractfile(member)
                        if f:
                            tmp_dest.write_bytes(f.read())
                            extracted = True
                        break
        if extracted:
            tmp_dest.chmod(0o755)
            # Before the replace, never after: `replace` is what destroys the
            # old binary, and after it there is nothing left to preserve.
            _preserve_outgoing(dest, installed, console)
            tmp_dest.replace(dest)
            console.print(f"  [green]omnigraph[/green] → {dest}")
        else:
            refuse(
                "  [red]omnigraph[/red] — binary not found in archive; "
                "install manually",
                f"no omnigraph binary inside {asset}; the release asset layout "
                "changed.",
            )
    except OmnigraphInstallFailed:
        # Already reported by `refuse`; re-wrapping it in the catch-all below
        # would bury the specific reason under a generic "download failed".
        raise
    except Exception as exc:  # noqa: BLE001
        refuse(
            f"  [red]omnigraph download failed[/red] ({exc}); install manually",
            f"omnigraph install failed: {exc}",
        )
    finally:
        tmp_dest.unlink(missing_ok=True)
