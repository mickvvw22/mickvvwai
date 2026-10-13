"""Profile-scoped backup target behavior used by dashboard WebDAV actions."""
from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import hermes_cli.backup as backup


def test_run_backup_uses_explicit_profile_home(tmp_path: Path, monkeypatch, capsys):
    default_home = tmp_path / "default-home"
    profile_home = tmp_path / "profiles" / "work"
    default_home.mkdir(parents=True)
    profile_home.mkdir(parents=True)
    (default_home / "default-marker.txt").write_text("default profile")
    (profile_home / "profile-marker.txt").write_text("selected profile")
    archive = tmp_path / "profile-backup.zip"
    monkeypatch.setattr(backup, "get_default_hermes_root", lambda: default_home)

    assert backup.run_backup(
        SimpleNamespace(output=str(archive), hermes_home=str(profile_home), keep=0)
    ) is True
    capsys.readouterr()

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert any(name.endswith("profile-marker.txt") for name in names)
    assert not any(name.endswith("default-marker.txt") for name in names)
