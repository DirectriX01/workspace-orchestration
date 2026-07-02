"""Real Google Drive client wrapping the sync API in async methods."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.services.google import _with_retry
from app.services.google.oauth import credentials_from_user

if TYPE_CHECKING:
    from app.db.models import User

_FILE_FIELDS = "id, name, mimeType, owners, modifiedTime, webViewLink"
_LIST_FIELDS = f"nextPageToken, files({_FILE_FIELDS})"
_FOLDER_MIME = "application/vnd.google-apps.folder"
# Google-native editor types whose content can be exported as plain text.
_EXPORTABLE_MIMES = frozenset(
    {
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.presentation",
        "application/vnd.google-apps.spreadsheet",
        "application/vnd.google-apps.drawing",
    }
)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an RFC-3339 timestamp string into a tz-aware datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalize(file: dict[str, Any], content: str = "") -> dict[str, Any]:
    """Convert a raw Drive file resource to the contract shape."""
    owners = file.get("owners") or []
    owner_email = owners[0].get("emailAddress", "") if owners else ""
    return {
        "id": file.get("id", ""),
        "name": file.get("name", ""),
        "mime_type": file.get("mimeType", ""),
        "owner_email": owner_email,
        "content": content,
        "web_link": file.get("webViewLink", ""),
        "modified_at": _parse_dt(file.get("modifiedTime")),
    }


class DriveClient:
    """Async Drive client backed by the real Google API."""

    def __init__(self, user: "User") -> None:
        self._user = user
        self._service: Any = None

    def _svc(self) -> Any:
        if self._service is None:
            creds = credentials_from_user(self._user)
            self._service = build(
                "drive", "v3", credentials=creds, cache_discovery=False
            )
        return self._service

    async def list_files(
        self, updated_after: datetime | None = None, max_results: int = 500
    ) -> list[dict]:
        svc = self._svc()
        query = "trashed = false"
        if updated_after is not None:
            query += f" and modifiedTime > '{updated_after.isoformat()}'"
        files: list[dict] = []
        page_token: str | None = None
        while len(files) < max_results:
            remaining = max_results - len(files)

            def _list(token: str | None = page_token, batch: int = remaining) -> dict:
                return (
                    svc.files()
                    .list(
                        q=query,
                        fields=_LIST_FIELDS,
                        pageSize=min(batch, 1000),
                        orderBy="modifiedTime desc",
                        pageToken=token,
                    )
                    .execute()
                )

            response = await _with_retry(_list)
            files.extend(_normalize(f) for f in response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return files[:max_results]

    async def get_file(self, file_id: str) -> dict:
        svc = self._svc()

        def _get() -> dict:
            return svc.files().get(fileId=file_id, fields=_FILE_FIELDS).execute()

        try:
            file = await _with_retry(_get)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                raise KeyError(f"{file_id} not found") from None
            raise
        content = ""
        if file.get("mimeType") in _EXPORTABLE_MIMES:
            content = await self._export_text(file_id)
        return _normalize(file, content=content)

    async def _export_text(self, file_id: str) -> str:
        """Export a Google-native file as plain text (first 4000 chars); "" on failure."""
        svc = self._svc()

        def _export() -> Any:
            return svc.files().export(fileId=file_id, mimeType="text/plain").execute()

        try:
            data = await _with_retry(_export)
        except HttpError:
            return ""
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        return str(data)[:4000]

    async def share_file(self, file_id: str, email: str, role: str = "reader") -> dict:
        svc = self._svc()

        def _create() -> dict:
            return (
                svc.permissions()
                .create(
                    fileId=file_id,
                    body={"type": "user", "role": role, "emailAddress": email},
                    sendNotificationEmail=False,
                )
                .execute()
            )

        try:
            permission = await _with_retry(_create)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                raise KeyError(f"{file_id} not found") from None
            raise
        return {
            "id": file_id,
            "permission_id": permission.get("id", ""),
            "email": email,
            "role": role,
            "status": "shared",
        }

    async def create_folder(self, name: str, parent_id: str | None = None) -> dict:
        svc = self._svc()
        body: dict[str, Any] = {"name": name, "mimeType": _FOLDER_MIME}
        if parent_id is not None:
            body["parents"] = [parent_id]

        def _create() -> dict:
            return svc.files().create(body=body, fields=_FILE_FIELDS).execute()

        return _normalize(await _with_retry(_create))

    async def move_file(self, file_id: str, folder_id: str) -> dict:
        svc = self._svc()

        def _get_parents() -> dict:
            return svc.files().get(fileId=file_id, fields="parents").execute()

        try:
            current = await _with_retry(_get_parents)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                raise KeyError(f"{file_id} not found") from None
            raise
        previous_parents = ",".join(current.get("parents", []))

        def _update() -> dict:
            return (
                svc.files()
                .update(
                    fileId=file_id,
                    addParents=folder_id,
                    removeParents=previous_parents,
                    fields="id, parents",
                )
                .execute()
            )

        updated = await _with_retry(_update)
        return {
            "id": file_id,
            "folder_id": folder_id,
            "parents": list(updated.get("parents", [])),
            "status": "moved",
        }
