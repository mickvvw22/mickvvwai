"""Telegram inbound file persistence, archive handling and retention lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from gateway.platforms.event import MessageEvent, MessageType
from plugins.platforms.telegram.telegram_file_storage import TelegramFileStorage

logger = logging.getLogger(__name__)

_INFERRED_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "application/json": ".json",
    "application/xml": ".xml",
    "application/yaml": ".yaml",
    "application/toml": ".toml",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/gzip": ".gz",
    "application/x-gzip": ".gz",
    "application/x-bzip2": ".bz2",
    "application/x-xz": ".xz",
    "application/x-tar": ".tar",
    "application/x-gtar": ".tar",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
}


class TelegramFileHandlingMixin:
    """Keep authorized Telegram attachments in durable app/profile-scoped storage."""

    def _telegram_file_storage(self) -> TelegramFileStorage:
        storage = getattr(self, "_file_storage", None)
        if storage is not None:
            return storage
        extra = getattr(getattr(self, "config", None), "extra", {}) or {}
        options = extra.get("file_handling", {}) if isinstance(extra, dict) else {}
        if not isinstance(options, dict):
            options = {}
        enabled = options.get("cleanup_enabled", False)
        if not isinstance(enabled, bool):
            enabled = str(enabled).strip().lower() in {"1", "true", "yes", "on"}
        try:
            cleanup_after_days = int(options.get("cleanup_after_days", 30))
        except (TypeError, ValueError):
            cleanup_after_days = 30
        profile_home = getattr(self, "_update_receipt_dir", None)
        if profile_home is None:
            from hermes_constants import get_hermes_home
            profile_home = get_hermes_home()
        storage_root = os.environ.get("HERMES_TELEGRAM_FILES_ROOT") or None
        installation_storage = os.environ.get("HERMES_TELEGRAM_FILES_INSTALL_DIR", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        storage = TelegramFileStorage(
            profile_home,
            cleanup_enabled=enabled,
            cleanup_after_days=cleanup_after_days,
            storage_root=storage_root,
            installation_storage=installation_storage,
        )
        self._file_storage = storage
        return storage

    def _start_file_cleanup(self) -> None:
        """Start one background retention worker when the user opts into cleanup."""
        storage = self._telegram_file_storage()
        if not storage.cleanup_enabled:
            return
        existing = getattr(self, "_file_cleanup_task", None)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._file_cleanup_loop(storage), name="telegram-file-cleanup")
        self._file_cleanup_task = task
        tracked = getattr(self, "_background_tasks", None)
        if isinstance(tracked, set):
            tracked.add(task)
            task.add_done_callback(tracked.discard)

    async def _file_cleanup_loop(self, storage: TelegramFileStorage) -> None:
        """Clean expired extracted bundles at startup and hourly; originals are never eligible."""
        while True:
            try:
                removed = await asyncio.to_thread(storage.cleanup_expired)
                if removed:
                    logger.info("[Telegram] Removed %d expired extracted-file folder(s)", removed)
            except OSError:
                logger.warning("[Telegram] Could not clean expired extracted files", exc_info=True)
            await asyncio.sleep(60 * 60)

    async def _store_received_attachment(self, data: bytes, filename: str, mime_type: str = "") -> str:
        """Persist original Telegram bytes under the configured durable Telegram-file root."""
        self._start_file_cleanup()
        path = await asyncio.to_thread(self._telegram_file_storage().store_received, data, filename, mime_type)
        return str(path)

    async def _cache_inbound_document(self, msg, event: MessageEvent) -> bool:
        """Persist any authorized Telegram document, and expose extracted content to the agent."""
        from gateway.platforms.base import (
            SUPPORTED_DOCUMENT_TYPES,
            SUPPORTED_IMAGE_DOCUMENT_TYPES,
            SUPPORTED_VIDEO_TYPES,
            _TEXT_INJECT_EXTENSIONS,
            cache_image_from_bytes_async,
        )

        doc = msg.document
        original_filename = doc.file_name or ""
        ext = Path(original_filename).suffix.lower() if original_filename else ""
        doc_mime = (doc.mime_type or "").split(";", 1)[0].strip().lower()
        image_mime_to_ext: dict[str, str] = {}
        for image_ext, image_mime in SUPPORTED_IMAGE_DOCUMENT_TYPES.items():
            image_mime_to_ext.setdefault(image_mime, image_ext)
        if not ext and doc_mime:
            ext = image_mime_to_ext.get(doc_mime, "")
            if not ext:
                ext = {mime: suffix for suffix, mime in SUPPORTED_VIDEO_TYPES.items()}.get(doc_mime, "")
            if not ext:
                ext = _INFERRED_MIME_EXTENSIONS.get(doc_mime, "")
        display = original_filename or doc_mime or ext or "unknown"
        if TelegramFileStorage.is_hidden_filename(original_filename):
            notice = f"Hidden file '{display}' was ignored; its contents were not read or shared."
            return await self._dispatch_with_text(event, f"{event.text}\n\n{notice}" if event.text else notice)
        size = getattr(doc, "file_size", None)
        if not size or size > self._max_doc_bytes:
            logger.info("[Telegram] Document too large or size unavailable: %s bytes", size)
            return await self._dispatch_with_text(
                event,
                f"The document is too large or its size could not be verified. Maximum: {self._max_doc_bytes // (1024 * 1024)} MB.",
            )

        try:
            file_obj = await doc.get_file()
            raw_bytes = bytes(await file_obj.download_as_bytearray())
            filename = original_filename or f"document{ext or '.bin'}"
            stored_path = await self._store_received_attachment(raw_bytes, filename, doc_mime)
            mime = doc_mime or SUPPORTED_DOCUMENT_TYPES.get(ext, "application/octet-stream")
            event.media_urls = [stored_path]
            event.media_types = [mime]
            event.media_text_inlined = [False]

            # Image documents are validated before entering the existing photo/vision path.
            if ext in image_mime_to_ext.values() or doc_mime.startswith("image/"):
                image_ext = ext if ext in image_mime_to_ext.values() else image_mime_to_ext.get(doc_mime, ".jpg")
                validation_path = await cache_image_from_bytes_async(raw_bytes, ext=image_ext)
                if validation_path:
                    with contextlib.suppress(OSError):
                        os.unlink(validation_path)
                event.media_types = [doc_mime if doc_mime.startswith("image/") else image_mime_to_ext.get(doc_mime, "image/jpeg")]
                event.message_type = MessageType.PHOTO
                logger.info("[Telegram] Saved user image-document at %s", stored_path)
                await self._route_photo_event(msg, event)
                return True

            if not ext and doc_mime:
                ext = {mime: suffix for suffix, mime in SUPPORTED_VIDEO_TYPES.items()}.get(doc_mime, "")
            if ext in SUPPORTED_VIDEO_TYPES or doc_mime.startswith("video/"):
                event.media_types = [doc_mime or SUPPORTED_VIDEO_TYPES.get(ext, "video/mp4")]
                event.message_type = MessageType.VIDEO
                logger.info("[Telegram] Saved user video document at %s", stored_path)
                await self.handle_message(event)
                return True

            if mime.startswith("audio/") or ext in {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".aac"}:
                event.message_type = MessageType.AUDIO

            # Extract archives into an independent folder; the original stays untouched in received/.
            storage = self._telegram_file_storage()
            try:
                extraction = await asyncio.to_thread(storage.extract_archive, Path(stored_path))
            except (OSError, ValueError) as exc:
                # A raw archive that cannot be inspected safely stays on disk only;
                # never hand it to the model where hidden entries could be read.
                event.media_urls = []
                event.media_types = []
                event.media_text_inlined = []
                event.text = self._prepend_file_notice(
                    event.text,
                    f"Archive '{display}' was retained locally but not shared because its contents could not be inspected safely ({type(exc).__name__}).",
                )
            else:
                if extraction is not None:
                    if extraction.skipped_hidden_count:
                        # Do not expose the original archive path: the model must not
                        # be able to unpack skipped hidden entries on its own.
                        if extraction.file_count:
                            event.media_urls = [str(extraction.directory)]
                            event.media_types = ["inode/directory"]
                            event.media_text_inlined = [None]
                        else:
                            event.media_urls = []
                            event.media_types = []
                            event.media_text_inlined = []
                    else:
                        event.media_urls.append(str(extraction.directory))
                        event.media_types.append("inode/directory")
                        event.media_text_inlined.append(None)
                    agent_path = str(extraction.directory)
                    notice = (
                        f"Archive '{display}' was safely extracted to: {agent_path}. "
                        f"It contains {extraction.file_count} eligible non-hidden file(s)."
                    )
                    if extraction.skipped_hidden_count:
                        notice += (
                            f" Skipped {extraction.skipped_hidden_count} hidden file/path entr(y/ies); "
                            "the original archive remains saved locally and was not shared in this conversation."
                        )
                        if not extraction.file_count:
                            notice += " No extracted files were attached."
                    if extraction.text_preview:
                        notice += f"\n\nReadable extracted text (bounded preview):\n{extraction.text_preview}"
                    event.text = self._prepend_file_notice(event.text, notice)

            # Inline small text/config/code files. Other formats remain available by their local paths.
            if ext in _TEXT_INJECT_EXTENSIONS or mime.startswith("text/"):
                if len(raw_bytes) <= 100 * 1024:
                    try:
                        text_content = raw_bytes.decode("utf-8-sig")
                        display_name = re.sub(r"[^\w.\- ]", "_", filename)
                        injection = f"[Content of {display_name}]:\n{text_content}"
                        event.text = f"{injection}\n\n{event.text}" if event.text else injection
                        event.media_text_inlined[0] = True
                    except UnicodeDecodeError:
                        pass
            logger.info("[Telegram] Saved user document at %s (%s)", stored_path, mime)
        except Exception as exc:
            # Import lazily to avoid an adapter/mixin import cycle. The adapter's
            # helper redacts bot tokens from transport errors before logging.
            from plugins.platforms.telegram.adapter import _redact_telegram_error_text

            logger.warning("[Telegram] Failed to store document: %s", _redact_telegram_error_text(exc), exc_info=True)
            await self._surface_media_cache_failure(msg, event, "attachment", exc, display_name=original_filename or None)
        return False

    @staticmethod
    def _prepend_file_notice(text: Optional[str], notice: str) -> str:
        """Place file metadata before the user's caption without replacing it."""
        return f"[Telegram file: {notice}]\n\n{text}" if text else f"[Telegram file: {notice}]"
