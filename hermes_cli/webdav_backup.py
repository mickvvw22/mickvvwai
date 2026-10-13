"""Small, dependency-free WebDAV client for encrypted remote backups."""
from __future__ import annotations

import base64
import http.client
import ipaddress
import os
import re
import socket
import ssl
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

_MAX_LISTING_BYTES = 4 * 1024 * 1024
_MAX_BACKUP_BYTES = 10 * 1024 * 1024 * 1024
_FILENAME_RE = re.compile(r"^hermes-cloud-backup-\d{8}T\d{6}Z-[0-9a-f]{8}\.hbackup$")
_PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8" ?>\n'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/>'
    b'<d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>'
)


class WebDAVError(RuntimeError):
    """A WebDAV operation failed without exposing credentials."""


class WebDAVClient:
    def __init__(self, url: str, username: str = "", password: str = "", *, timeout: float = 30.0):
        parsed = urllib.parse.urlsplit((url or "").strip())
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise WebDAVError("Enter a valid WebDAV URL beginning with https:// (or a private-network http:// URL).")
        if parsed.username is not None or parsed.password is not None:
            raise WebDAVError("Put WebDAV credentials in the username and password fields, not in the URL.")
        if parsed.query or parsed.fragment:
            raise WebDAVError("The WebDAV URL must not contain a query string or fragment.")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise WebDAVError("The WebDAV URL contains an invalid port.") from exc
        if not 1 <= port <= 65535:
            raise WebDAVError("The WebDAV URL contains an invalid port.")
        if parsed.scheme == "http":
            self._require_private_http_host(parsed.hostname, port)
        self.url = parsed
        self.username = username or ""
        self.password = password or ""
        self.timeout = timeout
        self.base_path = parsed.path or "/"
        if not self.base_path.endswith("/"):
            self.base_path += "/"
        self.port = port

    @staticmethod
    def _require_private_http_host(host: str, port: int) -> None:
        try:
            addresses = {
                ipaddress.ip_address(result[4][0].split("%", 1)[0])
                for result in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as exc:
            raise WebDAVError("The WebDAV host could not be resolved.") from exc
        if not addresses or not all(ip.is_private or ip.is_loopback or ip.is_link_local for ip in addresses):
            raise WebDAVError(
                "Unencrypted HTTP is allowed only for a private or local WebDAV server; use HTTPS otherwise."
            )

    def _connection(self) -> tuple[http.client.HTTPConnection, str]:
        host = self.url.hostname
        if self.url.scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, self.port, timeout=self.timeout, context=ssl.create_default_context()
            )
        else:
            connection = http.client.HTTPConnection(host, self.port, timeout=self.timeout)
        target = urllib.parse.urlunsplit(("", "", self.url.path or "/", "", ""))
        return connection, target

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Connection": "close"}
        if self.username or self.password:
            token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _check_status(method: str, status: int, reason: str, expected: tuple[int, ...]) -> None:
        if status not in expected:
            if status in (401, 403):
                raise WebDAVError("WebDAV authentication failed or access was denied.")
            if status == 404:
                raise WebDAVError("The WebDAV folder or backup file was not found.")
            raise WebDAVError(f"WebDAV {method} failed (HTTP {status} {reason}).")

    def _request(
        self, method: str, target: str, *, body: bytes | None = None,
        headers: dict[str, str] | None = None, expected: tuple[int, ...] = (200,),
    ) -> tuple[http.client.HTTPResponse, bytes]:
        connection, _ = self._connection()
        try:
            request_headers = self._headers(headers)
            if body is not None:
                request_headers.setdefault("Content-Length", str(len(body)))
            connection.request(method, target, body=body, headers=request_headers)
            response = connection.getresponse()
            self._check_status(method, response.status, response.reason, expected)
            data = response.read(_MAX_LISTING_BYTES + 1)
            if len(data) > _MAX_LISTING_BYTES:
                raise WebDAVError("The WebDAV server returned an unexpectedly large response.")
            return response, data
        except WebDAVError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise WebDAVError(f"Could not reach the WebDAV server ({type(exc).__name__}).") from exc
        finally:
            connection.close()

    def test(self) -> dict[str, Any]:
        response, _ = self._request(
            "PROPFIND", self.base_path, body=_PROPFIND_BODY,
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            expected=(200, 207),
        )
        return {"ok": True, "status": response.status}

    def list_backups(self) -> list[dict[str, Any]]:
        _, body = self._request(
            "PROPFIND", self.base_path, body=_PROPFIND_BODY,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            expected=(200, 207),
        )
        upper_body = body.upper()
        if b"<!DOCTYPE" in upper_body or b"<!ENTITY" in upper_body:
            raise WebDAVError("The WebDAV server returned an unsafe directory listing.")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise WebDAVError("The WebDAV server returned an invalid directory listing.") from exc
        dav = "{DAV:}"
        items: list[dict[str, Any]] = []
        for entry in root.findall(f"{dav}response"):
            href = entry.findtext(f"{dav}href") or ""
            name = urllib.parse.unquote(urllib.parse.urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1])
            if not _FILENAME_RE.fullmatch(name):
                continue
            prop = entry.find(f".//{dav}prop")
            if prop is None:
                continue
            try:
                size = int(prop.findtext(f"{dav}getcontentlength") or "0")
            except ValueError:
                size = 0
            items.append({
                "filename": name,
                "size": size,
                "modified": prop.findtext(f"{dav}getlastmodified") or "",
            })
        return sorted(items, key=lambda item: item["filename"], reverse=True)

    def put_file(self, filename: str, source: str | Path) -> int:
        if not _FILENAME_RE.fullmatch(filename):
            raise WebDAVError("Invalid backup filename.")
        path = Path(source)
        size = path.stat().st_size
        if size > _MAX_BACKUP_BYTES:
            raise WebDAVError("The local backup exceeds the maximum supported size.")
        target = self.base_path + urllib.parse.quote(filename, safe="")
        connection, _ = self._connection()
        try:
            connection.putrequest("PUT", target)
            upload_headers = self._headers({
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            })
            for key, value in upload_headers.items():
                connection.putheader(key, value)
            connection.endheaders()
            with path.open("rb") as src:
                while chunk := src.read(1024 * 1024):
                    connection.send(chunk)
            response = connection.getresponse()
            self._check_status("PUT", response.status, response.reason, (200, 201, 204))
            response.read(4096)
            return size
        except WebDAVError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise WebDAVError(f"Could not upload the encrypted backup ({type(exc).__name__}).") from exc
        finally:
            connection.close()

    def get_file(self, filename: str, destination: str | Path) -> int:
        if not _FILENAME_RE.fullmatch(filename):
            raise WebDAVError("Invalid backup filename.")
        target = self.base_path + urllib.parse.quote(filename, safe="")
        connection, _ = self._connection()
        path = Path(destination)
        try:
            connection.request("GET", target, headers=self._headers())
            response = connection.getresponse()
            self._check_status("GET", response.status, response.reason, (200,))
            declared = response.getheader("Content-Length")
            if declared:
                try:
                    if int(declared) > _MAX_BACKUP_BYTES:
                        raise WebDAVError("The remote backup exceeds the maximum supported size.")
                except ValueError:
                    raise WebDAVError("The WebDAV server returned an invalid content length.")
            written = 0
            with path.open("wb") as dst:
                os.chmod(path, 0o600)
                while chunk := response.read(1024 * 1024):
                    written += len(chunk)
                    if written > _MAX_BACKUP_BYTES:
                        raise WebDAVError("The remote backup exceeds the maximum supported size.")
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            return written
        except WebDAVError:
            path.unlink(missing_ok=True)
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            path.unlink(missing_ok=True)
            raise WebDAVError(f"Could not download the encrypted backup ({type(exc).__name__}).") from exc
        finally:
            connection.close()
