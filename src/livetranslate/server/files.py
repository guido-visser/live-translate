"""The app's own pages, scripts and icons, served from memory.

Starlette's FileResponse looks at the file in a worker thread. On 12 Nov 2026, after a very
slow start during Windows updates, that hop failed in the running app ("There is no current
event loop in thread 'server'") and every page gave an error while the JSON API kept working.
These files are small and only change with an update, so they are read once and then served
without a worker thread. A changed file (while developing) is read again.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response

TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}


@dataclass
class _File:
    path: Path
    mtime: int
    body: bytes
    etag: str
    media_type: str


class WebFiles:
    """Every file under one folder, by its path relative to that folder ("admin/admin.js")."""

    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, _File] = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                self.files[path.relative_to(root).as_posix()] = self._read(path)

    @staticmethod
    def _read(path: Path) -> _File:
        body = path.read_bytes()
        media_type = TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
        return _File(path, os.stat(path).st_mtime_ns, body, etag, media_type)

    def get(self, name: str) -> _File | None:
        found = self.files.get(name)
        if found is None:
            return None  # only files that were there at the start: no way out of the folder
        try:
            if os.stat(found.path).st_mtime_ns != found.mtime:
                found = self.files[name] = self._read(found.path)
        except OSError:
            pass  # gone or locked (an update replacing it): what was read still works
        return found

    def response(self, name: str, request: Request | None = None, media_type: str | None = None) -> Response:
        """The browser checks before reusing it (no-cache), so an update never mixes new pages with
        old scripts; an unchanged file then costs a 304 without a body."""
        found = self.get(name)
        if found is None:
            return Response("Not found", status_code=404, media_type="text/plain")
        headers = {"Cache-Control": "no-cache", "ETag": found.etag}
        if request is not None and found.etag in request.headers.get("if-none-match", ""):
            return Response(status_code=304, headers=headers)
        body = b"" if request is not None and request.method == "HEAD" else found.body
        response = Response(body, media_type=media_type or found.media_type, headers=headers)
        if body != found.body:
            response.headers["content-length"] = str(len(found.body))
        return response
