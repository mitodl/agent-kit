from agent_config_kit.installers import install_files, install_skills
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
