from agent_config_kit.installers import install_files


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
