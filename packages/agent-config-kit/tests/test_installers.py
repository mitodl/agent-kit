import pytest

from agent_config_kit.installers import (
    ConflictingPathError,
    hook_content_hash,
    install_files,
    install_skills,
    skill_content_hash,
    skill_files,
)
from agent_config_kit.models import SkillSource


def test_install_files_skips_directories_matching_the_suffix(tmp_path):
    """A directory literally named "*.sh" must not be passed to shutil.copy2,
    which would raise IsADirectoryError."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "real.sh").write_text("#!/bin/sh\necho hi\n")
    (src_dir / "not-a-file.sh").mkdir()
    dest_dir = tmp_path / "dest"

    dests = install_files(src_dir, dest_dir, suffix=".sh", dry_run=False)

    assert [d.name for d in dests] == ["real.sh"]
    assert (dest_dir / "real.sh").is_file()
    assert not (dest_dir / "not-a-file.sh").exists()


def _skill_with_supporting_files(src_dir) -> SkillSource:
    src_dir.mkdir(parents=True)
    (src_dir / "SKILL.md").write_text("# my-skill\n\nSee scripts/run.sh.\n")
    (src_dir / "scripts").mkdir()
    script = src_dir / "scripts" / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    (src_dir / "references").mkdir()
    (src_dir / "references" / "notes.md").write_text("# notes\n")
    return SkillSource(name="my-skill", skill_md_path=src_dir / "SKILL.md")


def test_install_skills_copies_scripts_and_references_alongside_skill_md(tmp_path):
    skill = _skill_with_supporting_files(tmp_path / "src")
    dest_dir = tmp_path / "dest"

    dests = install_skills([skill], [dest_dir], dry_run=False)

    skill_dest = dest_dir / "my-skill"
    assert (
        skill_dest / "SKILL.md"
    ).read_text() == "# my-skill\n\nSee scripts/run.sh.\n"
    assert (skill_dest / "scripts" / "run.sh").read_text() == "#!/bin/sh\necho hi\n"
    assert (skill_dest / "references" / "notes.md").read_text() == "# notes\n"
    assert set(dests) == {
        skill_dest / "SKILL.md",
        skill_dest / "scripts" / "run.sh",
        skill_dest / "references" / "notes.md",
    }


def test_install_skills_preserves_executable_permission_on_scripts(tmp_path):
    skill = _skill_with_supporting_files(tmp_path / "src")
    dest_dir = tmp_path / "dest"

    install_skills([skill], [dest_dir], dry_run=False)

    dest_script = dest_dir / "my-skill" / "scripts" / "run.sh"
    assert dest_script.stat().st_mode & 0o111  # still executable


def test_install_skills_dry_run_writes_nothing_but_reports_all_files(tmp_path):
    skill = _skill_with_supporting_files(tmp_path / "src")
    dest_dir = tmp_path / "dest"

    dests = install_skills([skill], [dest_dir], dry_run=True)

    assert not dest_dir.exists()
    assert len(dests) == 3


def test_install_skills_copies_to_every_dest_dir(tmp_path):
    skill = _skill_with_supporting_files(tmp_path / "src")
    dest_a = tmp_path / "dest-a"
    dest_b = tmp_path / "dest-b"

    install_skills([skill], [dest_a, dest_b], dry_run=False)

    assert (dest_a / "my-skill" / "scripts" / "run.sh").exists()
    assert (dest_b / "my-skill" / "scripts" / "run.sh").exists()


def test_install_skills_raises_on_dangling_symlink_at_dest(tmp_path):
    """A leftover (e.g. from an older symlink-based install) dangling
    symlink occupying a skill's destination directory must not surface as a
    raw FileExistsError from deep inside pathlib."""
    skill = _skill_with_supporting_files(tmp_path / "src")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "my-skill").symlink_to(tmp_path / "nonexistent-target")

    with pytest.raises(ConflictingPathError, match="my-skill"):
        install_skills([skill], [dest_dir], dry_run=False)


def test_install_skills_force_replaces_dangling_symlink_at_dest(tmp_path):
    skill = _skill_with_supporting_files(tmp_path / "src")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "my-skill").symlink_to(tmp_path / "nonexistent-target")

    install_skills([skill], [dest_dir], dry_run=False, force=True)

    assert (dest_dir / "my-skill" / "SKILL.md").is_file()
    assert not (dest_dir / "my-skill").is_symlink()


def test_install_skills_single_file_skill_still_works(tmp_path):
    """A skill with no supporting files (just SKILL.md) must still install
    exactly that one file — no regression for the common case."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "SKILL.md").write_text("# skill")
    skill = SkillSource(name="my-skill", skill_md_path=src_dir / "SKILL.md")
    dest_dir = tmp_path / "dest"

    dests = install_skills([skill], [dest_dir], dry_run=False)

    assert dests == [dest_dir / "my-skill" / "SKILL.md"]


def test_skill_files_raises_if_skill_md_path_does_not_exist(tmp_path):
    """Silently yielding an empty file list here would make apply()/diff()
    quietly skip the whole skill instead of surfacing the mistake."""
    skill = SkillSource(
        name="my-skill", skill_md_path=tmp_path / "does-not-exist" / "SKILL.md"
    )

    with pytest.raises(FileNotFoundError):
        skill_files(skill)


def test_skill_files_rejects_unsafe_name_even_if_constructed_bypassing_validation(
    tmp_path,
):
    """SkillSource.name is validated at construction, but skill_files() (the
    one place a name is actually used to build a filesystem path) re-checks
    independently — this covers a SkillSource built via model_construct()
    (bypasses validators) or any other path that doesn't go through
    SkillSource.__init__."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "SKILL.md").write_text("# skill")
    skill = SkillSource.model_construct(
        name="../../etc", skill_md_path=src_dir / "SKILL.md"
    )

    with pytest.raises(ValueError, match="unsafe or invalid skill name"):
        skill_files(skill)


def test_skill_content_hash_stable_across_calls(tmp_path):
    skill = _skill_with_supporting_files(tmp_path / "src")

    assert skill_content_hash(skill) == skill_content_hash(skill)


def test_skill_content_hash_changes_when_a_file_changes(tmp_path):
    skill = _skill_with_supporting_files(tmp_path / "src")
    before = skill_content_hash(skill)

    (tmp_path / "src" / "scripts" / "run.sh").write_text("#!/bin/sh\necho changed\n")

    assert skill_content_hash(skill) != before


def test_skill_content_hash_changes_when_a_supporting_file_is_added(tmp_path):
    skill = _skill_with_supporting_files(tmp_path / "src")
    before = skill_content_hash(skill)

    (tmp_path / "src" / "scripts" / "new.sh").write_text("#!/bin/sh\necho new\n")

    assert skill_content_hash(skill) != before


def test_skill_content_hash_distinguishes_content_moved_between_files(tmp_path):
    """Two skills whose *concatenated* bytes are identical but split across
    files differently must not hash the same — proves the hash folds in
    each file's relative path, not just its bytes."""
    skill_a_dir = tmp_path / "a"
    skill_a_dir.mkdir()
    (skill_a_dir / "SKILL.md").write_text("AB")
    skill_a = SkillSource(name="a", skill_md_path=skill_a_dir / "SKILL.md")

    skill_b_dir = tmp_path / "b"
    skill_b_dir.mkdir()
    (skill_b_dir / "SKILL.md").write_text("A")
    (skill_b_dir / "extra.txt").write_text("B")
    skill_b = SkillSource(name="b", skill_md_path=skill_b_dir / "SKILL.md")

    assert skill_content_hash(skill_a) != skill_content_hash(skill_b)


def test_hook_content_hash_stable_and_sensitive_to_content(tmp_path):
    entry_path = tmp_path / "witan.ts"
    entry_path.write_text("console.log('v1')")
    before = hook_content_hash(entry_path)

    assert hook_content_hash(entry_path) == before

    entry_path.write_text("console.log('v2')")

    assert hook_content_hash(entry_path) != before
