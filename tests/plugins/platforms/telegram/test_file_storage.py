from __future__ import annotations

import gzip
import io
import os
import tarfile
import time
import zipfile
from pathlib import Path

import pytest

from plugins.platforms.telegram.telegram_file_storage import (
    TelegramFileStorage,
    UnsupportedArchiveError,
    UnsafeArchiveError,
)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_received_files_are_categorized_and_hidden_names_are_detected(tmp_path: Path) -> None:
    storage = TelegramFileStorage(tmp_path / "profile")
    env_file = storage.store_received(b"TOKEN=x", ".env", "text/plain")
    pdf_file = storage.store_received(b"pdf", "report.pdf", "application/pdf")
    image_file = storage.store_received(b"image", "upload.bin", "image/png")
    archive_file = storage.store_received(b"zip", "upload.bin", "application/zip")
    audio_file = storage.store_received(b"audio", "upload.bin", "audio/mpeg")

    assert storage.is_hidden_filename(".secret")
    assert storage.is_hidden_filename("folder/.secret")
    assert storage.is_hidden_filename("folder/.hidden_dir/file.txt")
    assert not storage.is_hidden_filename("folder/visible.txt")
    assert env_file.parent.name == "data"
    assert env_file.name.endswith("_.env")
    assert env_file.read_bytes() == b"TOKEN=x"
    assert pdf_file.parent.name == "documents"
    assert pdf_file.read_bytes() == b"pdf"
    assert image_file.parent.name == "images"
    assert archive_file.parent.name == "archives"
    assert audio_file.parent.name == "audio"


def test_zip_skips_hidden_names_and_paths_but_reads_visible_files(tmp_path: Path) -> None:
    storage = TelegramFileStorage(tmp_path / "profile")
    original = storage.store_received(
        _zip_bytes({
            "docs/readme.md": b"hello from archive",
            ".secret": b"root hidden marker",
            "folder/.secret": b"nested hidden marker",
            ".hidden_dir/": b"",
            ".hidden_dir/private.txt": b"hidden directory marker",
            "folder/.hidden_dir/private.txt": b"nested hidden directory marker",
        }),
        "bundle.zip",
        "application/zip",
    )

    result = storage.extract_archive(original)

    assert result is not None
    assert result.file_count == 1
    assert result.skipped_hidden_count == 5
    assert result.directory.parent == storage.extracted_root
    assert (result.directory / "documents" / "docs" / "readme.md").read_bytes() == b"hello from archive"
    assert "hello from archive" in result.text_preview
    assert not any(marker in result.text_preview for marker in ("root hidden marker", "nested hidden marker", "hidden directory marker"))
    assert not (result.directory / "folder").exists()
    assert not (result.directory / ".hidden_dir").exists()
    assert all(not path.name.startswith(".") for path in result.directory.rglob("*"))
    assert original.is_file()  # extraction never moves or deletes the received original


@pytest.mark.parametrize("member", ["../escape.txt", "folder/../escape.txt"])
def test_zip_path_traversal_is_rejected_and_original_is_retained(tmp_path: Path, member: str) -> None:
    storage = TelegramFileStorage(tmp_path / "profile")
    original = storage.store_received(_zip_bytes({member: b"bad"}), "bad.zip", "application/zip")

    with pytest.raises(UnsafeArchiveError, match="path traversal"):
        storage.extract_archive(original)

    assert original.is_file()
    assert not (tmp_path / "profile" / "cache" / "telegram_files" / "escape.txt").exists()
    assert list(storage.extracted_root.iterdir()) == []


def test_uninspectable_archive_format_stays_local(tmp_path: Path) -> None:
    storage = TelegramFileStorage(tmp_path / "profile")
    original = storage.store_received(b"opaque 7z payload", "bundle.7z", "application/x-7z-compressed")

    with pytest.raises(UnsupportedArchiveError):
        storage.extract_archive(original)

    assert original.is_file()
    assert not storage.extracted_root.exists()


def test_zip_symbolic_links_are_rejected(tmp_path: Path) -> None:
    storage = TelegramFileStorage(tmp_path / "profile")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        link = zipfile.ZipInfo("docs/link.txt")
        link.create_system = 3
        link.external_attr = (0o120777 << 16)
        archive.writestr(link, "../../outside")
    original = storage.store_received(buffer.getvalue(), "links.zip", "application/zip")

    with pytest.raises(UnsafeArchiveError, match="symbolic link"):
        storage.extract_archive(original)


def test_tar_skips_hidden_paths_without_exposing_them_to_the_preview(tmp_path: Path) -> None:
    storage = TelegramFileStorage(tmp_path / "profile")
    tar_bytes = io.BytesIO()
    entries = {
        "docs/readme.txt": b"visible tar content",
        ".secret": b"hidden tar content",
        "folder/.private/secret.txt": b"hidden directory content",
    }
    with tarfile.open(fileobj=tar_bytes, mode="w") as archive:
        for name, payload in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    original = storage.store_received(tar_bytes.getvalue(), "bundle.tar", "application/x-tar")

    result = storage.extract_archive(original)

    assert result is not None
    assert result.file_count == 1
    assert result.skipped_hidden_count == 2
    assert (result.directory / "documents" / "docs" / "readme.txt").read_bytes() == b"visible tar content"
    assert "visible tar content" in result.text_preview
    assert "hidden tar content" not in result.text_preview
    assert not (result.directory / ".secret").exists()
    assert not (result.directory / "folder" / ".private").exists()
    assert original.is_file()


def test_tar_and_single_gzip_archives_are_supported(tmp_path: Path) -> None:
    storage = TelegramFileStorage(tmp_path / "profile")
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w:gz") as archive:
        payload = b"from tar"
        member = tarfile.TarInfo("notes.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    tar_original = storage.store_received(tar_bytes.getvalue(), "backup.tar.gz", "application/gzip")
    tar_result = storage.extract_archive(tar_original)
    assert tar_result is not None
    assert (tar_result.directory / "documents" / "notes.txt").read_bytes() == b"from tar"

    compressed_original = storage.store_received(gzip.compress(b"from gzip"), "notes.txt.gz", "application/gzip")
    gzip_result = storage.extract_archive(compressed_original)
    assert gzip_result is not None
    assert (gzip_result.directory / "documents" / "notes.txt").read_bytes() == b"from gzip"


def test_cleanup_is_opt_in_and_only_removes_expired_extraction_folders(tmp_path: Path) -> None:
    home = tmp_path / "profile"
    disabled = TelegramFileStorage(home, cleanup_enabled=False, cleanup_after_days=1)
    original = disabled.store_received(b"archive", "kept.zip", "application/zip")
    old_dir = disabled.extracted_root / "old_bundle"
    old_dir.mkdir(parents=True)
    (old_dir / "file.txt").write_text("keep when disabled", encoding="utf-8")
    old = time.time() - 3 * 24 * 60 * 60
    os.utime(old_dir, (old, old))

    assert disabled.cleanup_expired() == 0
    assert old_dir.exists()

    enabled = TelegramFileStorage(home, cleanup_enabled=True, cleanup_after_days=1)
    assert enabled.cleanup_expired() == 1
    assert not old_dir.exists()
    assert original.is_file()
    assert original.read_bytes() == b"archive"


def test_installation_storage_never_uses_automatic_cleanup(tmp_path: Path) -> None:
    storage = TelegramFileStorage(
        tmp_path / "profile",
        cleanup_enabled=True,
        cleanup_after_days=1,
        storage_root=tmp_path / "Mickvvw-data" / "telegram_files",
        installation_storage=True,
    )
    original = storage.store_received(b"original", "bundle.zip", "application/zip")
    old_dir = storage.extracted_root / "old_bundle"
    old_dir.mkdir(parents=True)
    (old_dir / "file.txt").write_text("persistent", encoding="utf-8")
    old = time.time() - 3 * 24 * 60 * 60
    os.utime(old_dir, (old, old))

    assert storage.cleanup_enabled is False
    assert storage.cleanup_expired() == 0
    assert old_dir.exists()
    assert original.is_file()


def test_non_archives_are_kept_without_creating_extraction_directories(tmp_path: Path) -> None:
    storage = TelegramFileStorage(tmp_path / "profile")
    source = storage.store_received(b"ordinary document", "memo.txt", "text/plain")

    assert storage.extract_archive(source) is None
    assert source.is_file()
    assert not storage.extracted_root.exists()
