"""Profile-scoped storage and safe extraction for Telegram inbound files."""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import re
import shutil
import stat
import tarfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

_MAX_ARCHIVE_FILES = 2_000
_MAX_UNPACKED_BYTES = 512 * 1024 * 1024
_MAX_PREVIEW_BYTES = 180 * 1024
_MAX_PREVIEW_FILE_BYTES = 24 * 1024
_READABLE_EXTENSIONS = {
    ".txt", ".md", ".rst", ".csv", ".tsv", ".log", ".json", ".jsonl", ".xml", ".html", ".htm",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".java", ".c", ".h", ".cpp", ".rs", ".go", ".sh", ".ps1", ".sql", ".properties", ".env",
}
_CATEGORY_EXTENSIONS = {
    "images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".heic"},
    "audio": {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac", ".wma"},
    "video": {".mp4", ".mov", ".mkv", ".avi", ".webm", ".3gp", ".m4v"},
    "archives": {".zip", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz", ".tbz2", ".txz", ".7z", ".rar", ".zipx", ".cab", ".arj", ".lzh", ".lha", ".zst", ".zstd", ".lz4", ".lzma", ".lz", ".lzo", ".br", ".cpio", ".iso"},
    "code": {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".rs", ".go", ".sh", ".ps1", ".sql"},
    "documents": {".pdf", ".doc", ".docx", ".rtf", ".odt", ".md", ".txt", ".rst"},
    "spreadsheets": {".xls", ".xlsx", ".ods", ".csv", ".tsv"},
    "presentations": {".ppt", ".pptx", ".odp"},
    "data": {".json", ".jsonl", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env"},
    "executables": {".exe", ".msi", ".app", ".dmg", ".deb", ".rpm", ".apk"},
}
_UNSUPPORTED_ARCHIVE_SUFFIXES = (
    ".tar.zst", ".tzst", ".rar", ".7z", ".zipx", ".cab", ".arj", ".lzh", ".lha",
    ".zst", ".zstd", ".lz4", ".lzma", ".lz", ".lzo", ".br", ".cpio", ".iso",
)
_CATEGORY_MIME_TYPES = {
    "archives": {
        "application/zip", "application/x-zip-compressed", "application/x-tar", "application/x-gtar",
        "application/gzip", "application/x-gzip", "application/x-bzip2", "application/x-xz",
        "application/x-7z-compressed", "application/vnd.rar", "application/x-rar-compressed",
    },
    "documents": {"application/pdf", "application/msword", "application/rtf"},
    "spreadsheets": {
        "text/csv", "text/tab-separated-values", "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    },
    "presentations": {
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    },
    "data": {"application/json", "application/xml", "application/yaml", "application/toml"},
    "executables": {"application/x-msdownload", "application/vnd.microsoft.portable-executable"},
}


@dataclass(frozen=True)
class ArchiveExtraction:
    """One extracted archive directory and a bounded text preview for the agent."""

    directory: Path
    file_count: int
    text_preview: str
    skipped_hidden_count: int = 0


class UnsafeArchiveError(ValueError):
    """An archive contains unsafe paths, links, or exceeds extraction limits."""


class HiddenArchiveEntry(ValueError):
    """An archive path includes a hidden component and must not be exposed."""


class UnsupportedArchiveError(ValueError):
    """An archive format cannot be inspected safely by the built-in extractor."""


class TelegramFileStorage:
    """Store original uploads separately from extracted archive contents.

    Desktop installs may provide an app-specific durable root outside the profile cache;
    that root is separately mounted for agent reads. Otherwise the profile cache is used.
    Only extracted directories under a cleanup-eligible root can be removed by retention.
    """

    def __init__(
        self,
        profile_home: Path | str,
        *,
        cleanup_enabled: bool = False,
        cleanup_after_days: int = 30,
        storage_root: Path | str | None = None,
        installation_storage: bool = False,
    ):
        self.root = (
            Path(storage_root).expanduser()
            if storage_root
            else Path(profile_home) / "cache" / "telegram_files"
        )
        self.installation_storage = bool(installation_storage)
        self.received_root = self.root / "received"
        self.extracted_root = self.root / "extracted" / "archives"
        self.cleanup_enabled = bool(cleanup_enabled) and not self.installation_storage
        self.cleanup_after_days = max(1, min(int(cleanup_after_days), 3650))

    @staticmethod
    def category_for(filename: str, mime_type: str = "") -> str:
        """Categorize by a known extension first, then use MIME for unknown suffixes."""
        name = Path(filename).name.lower()
        suffix = Path(name).suffix
        if not suffix and name.startswith("."):
            suffix = name
        for category, extensions in _CATEGORY_EXTENSIONS.items():
            if suffix in extensions:
                return category
        mime = str(mime_type or "").split(";", 1)[0].strip().lower()
        for category, prefix in (("images", "image/"), ("audio", "audio/"), ("video", "video/")):
            if mime.startswith(prefix):
                return category
        for category, mime_types in _CATEGORY_MIME_TYPES.items():
            if mime in mime_types:
                return category
        if mime.startswith("text/"):
            return "documents"
        return "other"

    @staticmethod
    def is_hidden_filename(filename: str) -> bool:
        """Whether a file name/path contains a hidden component such as `.env`."""
        normalized = str(filename or "").replace("\\", "/")
        return any(part.startswith(".") and part not in {".", ".."} for part in normalized.split("/"))

    @staticmethod
    def safe_filename(filename: str, fallback: str = "document.bin") -> str:
        """Reduce a user-controlled filename to one harmless path component."""
        name = str(filename or "").replace("\\", "/").split("/")[-1]
        name = "".join(ch for ch in name if ch.isprintable() and ch not in "\x00")
        name = re.sub(r"[<>:\"|?*]", "_", name).strip().rstrip(".")
        if name in {"", ".", ".."}:
            name = fallback
        return name[:180]

    def store_received(self, data: bytes, filename: str, mime_type: str = "") -> Path:
        """Atomically save the original bytes under received/<category>/ without overwriting."""
        safe_name = self.safe_filename(filename)
        category = self.category_for(safe_name, mime_type)
        folder = self.received_root / category
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"tg_{uuid.uuid4().hex[:12]}_{safe_name}"
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.part")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target

    def extract_archive(self, source: Path | str) -> ArchiveExtraction | None:
        """Safely extract ZIP/TAR and single-file gzip/bzip2/xz archives.

        Formats without a built-in safe decoder (including RAR and 7z) are retained
        locally but must not be passed to the agent because hidden entries cannot be filtered.
        """
        archive = Path(source)
        source_name = self._source_filename(archive.name)
        if self.is_hidden_filename(source_name):
            raise UnsafeArchiveError("hidden files are not read or shared")
        suffix = source_name.lower()
        if suffix.endswith(_UNSUPPORTED_ARCHIVE_SUFFIXES):
            raise UnsupportedArchiveError("this archive format cannot be inspected safely; the original stays local and is not shared")
        kind = self._archive_kind(archive)
        if kind is None:
            return None
        base = self._archive_stem(archive.name)
        directory = self.extracted_root / f"{self.safe_filename(base, 'archive')}_{uuid.uuid4().hex[:10]}"
        directory.mkdir(parents=True, exist_ok=False)
        try:
            if kind == "zip":
                files, skipped_hidden = self._extract_zip(archive, directory)
            elif kind == "tar":
                files, skipped_hidden = self._extract_tar(archive, directory)
            else:
                files = [self._extract_single_compressed(archive, directory, kind)]
                skipped_hidden = 0
            return ArchiveExtraction(
                directory=directory,
                file_count=len(files),
                text_preview=self._text_preview(files),
                skipped_hidden_count=skipped_hidden,
            )
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    @staticmethod
    def _archive_kind(archive: Path) -> str | None:
        try:
            if zipfile.is_zipfile(archive):
                return "zip"
            if tarfile.is_tarfile(archive):
                return "tar"
        except (OSError, tarfile.TarError, zipfile.BadZipFile):
            return None
        suffix = archive.name.lower()
        if suffix.endswith(".gz"):
            return "gzip"
        if suffix.endswith(".bz2"):
            return "bzip2"
        if suffix.endswith(".xz"):
            return "xz"
        return None

    @staticmethod
    def _source_filename(filename: str) -> str:
        """Undo the storage-only ``tg_<12 hex>_`` prefix before naming extracted items."""
        parts = filename.split("_", 2)
        if len(parts) == 3 and parts[0] == "tg" and re.fullmatch(r"[0-9a-f]{12}", parts[1]):
            return parts[2]
        return filename

    @classmethod
    def _archive_stem(cls, filename: str) -> str:
        filename = cls._source_filename(filename)
        lowered = filename.lower()
        for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tbz2", ".tbz", ".tgz", ".txz", ".zip", ".tar", ".gz", ".bz2", ".xz"):
            if lowered.endswith(suffix):
                return filename[: -len(suffix)] or "archive"
        return Path(filename).stem or "archive"

    @classmethod
    def _safe_member_parts(cls, member: str) -> tuple[str, ...]:
        normalized = str(member).replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or re.match(r"^[A-Za-z]:", normalized) or "\x00" in normalized:
            raise UnsafeArchiveError(f"archive contains an absolute path: {member!r}")
        parts = tuple(part for part in path.parts if part not in {"", "."})
        if not parts or any(part == ".." for part in parts):
            raise UnsafeArchiveError(f"archive contains a path traversal entry: {member!r}")
        if any(part.startswith(".") for part in parts):
            raise HiddenArchiveEntry(f"archive contains a hidden path entry: {member!r}")
        return tuple(cls.safe_filename(part, "unnamed") for part in parts)

    @classmethod
    def _target_for_member(cls, root: Path, member: str) -> Path:
        parts = cls._safe_member_parts(member)
        category = cls.category_for(parts[-1])
        target = root / category
        for part in parts:
            target /= part
        resolved_root = root.resolve()
        resolved_target = target.resolve()
        if resolved_root not in resolved_target.parents:
            raise UnsafeArchiveError(f"archive entry escapes extraction directory: {member!r}")
        return target

    @staticmethod
    def _copy_bounded(source: BinaryIO, target: Path, *, expected_size: int, remaining: int) -> int:
        if expected_size < 0 or expected_size > remaining:
            raise UnsafeArchiveError("archive exceeds the 512 MiB extraction limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with target.open("xb") as output:
            while chunk := source.read(1024 * 1024):
                written += len(chunk)
                if written > expected_size or written > remaining:
                    raise UnsafeArchiveError("archive expands beyond its declared or allowed size")
                output.write(chunk)
        if written != expected_size:
            raise UnsafeArchiveError("archive entry size did not match its declared size")
        return written

    @classmethod
    def _extract_zip(cls, archive: Path, root: Path) -> tuple[list[Path], int]:
        files: list[Path] = []
        skipped_hidden = 0
        total = 0
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            if len(entries) > _MAX_ARCHIVE_FILES:
                raise UnsafeArchiveError("archive contains more than 2,000 entries")
            for info in entries:
                try:
                    cls._safe_member_parts(info.filename)
                except HiddenArchiveEntry:
                    skipped_hidden += 1
                    continue
                if info.is_dir():
                    cls._target_for_member(root, info.filename).mkdir(parents=True, exist_ok=True)
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise UnsafeArchiveError("archive contains a symbolic link")
                target = cls._target_for_member(root, info.filename)
                with bundle.open(info, "r") as source:
                    total += cls._copy_bounded(source, target, expected_size=info.file_size, remaining=_MAX_UNPACKED_BYTES - total)
                files.append(target)
        return files, skipped_hidden

    @classmethod
    def _extract_tar(cls, archive: Path, root: Path) -> tuple[list[Path], int]:
        files: list[Path] = []
        skipped_hidden = 0
        total = 0
        with tarfile.open(archive, mode="r:*") as bundle:
            entries = bundle.getmembers()
            if len(entries) > _MAX_ARCHIVE_FILES:
                raise UnsafeArchiveError("archive contains more than 2,000 entries")
            for member in entries:
                try:
                    cls._safe_member_parts(member.name)
                except HiddenArchiveEntry:
                    skipped_hidden += 1
                    continue
                if member.isdir():
                    cls._target_for_member(root, member.name).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise UnsafeArchiveError("archive contains a link or non-regular file")
                target = cls._target_for_member(root, member.name)
                source = bundle.extractfile(member)
                if source is None:
                    raise UnsafeArchiveError("archive entry could not be read")
                with source:
                    total += cls._copy_bounded(source, target, expected_size=member.size, remaining=_MAX_UNPACKED_BYTES - total)
                files.append(target)
        return files, skipped_hidden

    @classmethod
    def _extract_single_compressed(cls, archive: Path, root: Path, kind: str) -> Path:
        name = cls._source_filename(archive.name)
        lowered = name.lower()
        for suffix in (".gz", ".bz2", ".xz"):
            if lowered.endswith(suffix):
                name = name[:-len(suffix)] or "decompressed-file"
                break
        target = cls._target_for_member(root, name)
        opener = {"gzip": gzip.open, "bzip2": bz2.open, "xz": lzma.open}[kind]
        total = 0
        target.parent.mkdir(parents=True, exist_ok=True)
        with opener(archive, "rb") as source, target.open("xb") as output:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > _MAX_UNPACKED_BYTES:
                    raise UnsafeArchiveError("compressed file expands beyond the 512 MiB limit")
                output.write(chunk)
        return target

    @staticmethod
    def _text_preview(files: list[Path]) -> str:
        chunks: list[str] = []
        remaining = _MAX_PREVIEW_BYTES
        for path in files:
            if remaining <= 0 or path.suffix.lower() not in _READABLE_EXTENSIONS:
                continue
            try:
                with path.open("rb") as stream:
                    data = stream.read(min(_MAX_PREVIEW_FILE_BYTES, remaining) + 1)
                clipped = len(data) > min(_MAX_PREVIEW_FILE_BYTES, remaining)
                data = data[: min(_MAX_PREVIEW_FILE_BYTES, remaining)]
                try:
                    text = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = data.decode("cp1252", errors="replace")
                label = path.name
                chunks.append(f"--- {label}{' (preview truncated)' if clipped else ''} ---\n{text}")
                remaining -= len(data)
            except (OSError, UnicodeError):
                continue
        return "\n\n".join(chunks)

    def cleanup_expired(self, *, now: float | None = None) -> int:
        """Delete only expired extraction bundle directories; never remove originals."""
        if not self.cleanup_enabled or not self.extracted_root.is_dir():
            return 0
        current_time = time.time() if now is None else float(now)
        cutoff = current_time - self.cleanup_after_days * 24 * 60 * 60
        removed = 0
        for candidate in self.extracted_root.iterdir():
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                if candidate.stat(follow_symlinks=False).st_mtime < cutoff:
                    shutil.rmtree(candidate)
                    removed += 1
            except FileNotFoundError:
                continue
        return removed
