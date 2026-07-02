"""Drive agent: search, single-file fetch, and share/folder/move mutations."""

from __future__ import annotations

from typing import ClassVar

from app.agents.base import BaseAgent


class DriveAgent(BaseAgent):
    """Adapter over the hybrid searcher and a normalized Drive client."""

    name: ClassVar[str] = "drive"

    async def search(self, params: dict) -> dict:
        rows = await self.deps.searcher.search_gdrive(self.deps.user.id, params)
        return self._search_result(rows)

    async def get_context(self, params: dict) -> dict:
        file_id = params.get("file_id") or params.get("id")
        cached = await self.deps.searcher.get_by_source_id(
            "gdrive", self.deps.user.id, file_id
        )
        if cached is not None:
            return {"status": "ok", "results": [cached]}
        doc = await self.deps.client.get_file(file_id)
        result = self._jsonify(dict(doc))
        result["source"] = "gdrive"
        return {"status": "ok", "results": [result]}

    async def execute(self, action: str, params: dict) -> dict:
        if action == "share_file":
            payload = await self.deps.client.share_file(
                params["file_id"],
                params["email"],
                role=params.get("role", "reader") or "reader",
            )
        elif action == "create_folder":
            payload = await self.deps.client.create_folder(
                params["name"], parent_id=params.get("parent_id")
            )
        elif action == "move_file":
            payload = await self.deps.client.move_file(
                params["file_id"], params["folder_id"]
            )
        else:
            raise ValueError(f"unknown drive action: {action}")
        return self._ok(payload)
