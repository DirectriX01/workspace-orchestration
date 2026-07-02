"""Gmail agent: search, single-message fetch, and draft/send/label mutations."""

from __future__ import annotations

from typing import ClassVar

from app.agents.base import BaseAgent


class GmailAgent(BaseAgent):
    """Adapter over the hybrid searcher and a normalized Gmail client."""

    name: ClassVar[str] = "gmail"

    async def search(self, params: dict) -> dict:
        rows = await self.deps.searcher.search_gmail(self.deps.user.id, params)
        return self._search_result(rows)

    async def get_context(self, params: dict) -> dict:
        email_id = params.get("email_id") or params.get("id")
        cached = await self.deps.searcher.get_by_source_id(
            "gmail", self.deps.user.id, email_id
        )
        if cached is not None:
            return {"status": "ok", "results": [cached]}
        doc = await self.deps.client.get_message(email_id)
        result = self._jsonify(dict(doc))
        result["source"] = "gmail"
        return {"status": "ok", "results": [result]}

    async def execute(self, action: str, params: dict) -> dict:
        if action == "draft_email":
            payload = await self.deps.client.create_draft(
                to=self._as_list(params.get("to")),
                subject=params.get("subject", "") or "",
                body=params.get("body", "") or "",
            )
        elif action == "send_email":
            payload = await self.deps.client.send_message(
                to=self._as_list(params.get("to")),
                subject=params.get("subject", "") or "",
                body=params.get("body", "") or "",
            )
        elif action == "update_labels":
            payload = await self.deps.client.update_labels(
                params["email_id"],
                add=self._as_list(params.get("add")),
                remove=self._as_list(params.get("remove")),
            )
        else:
            raise ValueError(f"unknown gmail action: {action}")
        return self._ok(payload)
