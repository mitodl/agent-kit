# Changelog fragments

Each user-visible change gets its own fragment file here instead of an edit
to `../CHANGELOG.md` — parallel branches editing the same lines in one shared
file is what caused the version/changelog drift `bin/check_versions.py`
exists to catch.

Add one with `just changelog witan-core` (or `uvx scriv@1.8.0 create` from
this directory's parent), fill in whichever category section(s) apply, and
commit the file alongside your change. `just bump witan-core <part>` folds
every pending fragment into `../CHANGELOG.md` under the new version heading
and deletes them — it refuses to run if this directory holds nothing but this
README.
