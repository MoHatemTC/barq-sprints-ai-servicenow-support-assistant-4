import httpx
from typing import Any

from .config import settings


class ServiceNowClient:
    def __init__(self) -> None:
        self.base_url = settings.servicenow_instance_url.rstrip("/")
        self.auth = (
            settings.servicenow_username,
            settings.servicenow_password,
        )
        self.timeout = 20.0

    async def get_incident(self, sys_id: str) -> dict[str, Any]:
        """
        Fetch one incident's core fields by sys_id.

        Used by the S3.4 worker's deterministic preload step - the incident
        is fetched once here, before the agent runs, and its text is treated
        as untrusted data in the prompt (see agent/s3_worker.py).
        """
        url = f"{self.base_url}/api/now/table/incident/{sys_id}"
        params = {
            "sysparm_display_value": "true",
            "sysparm_fields": "sys_id,number,short_description,description,category",
        }
        async with httpx.AsyncClient(
            auth=self.auth,
            timeout=self.timeout,
            headers={"Accept": "application/json"},
        ) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        return data.get("result", {})

    async def update_incident(self, sys_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """
        PATCH arbitrary fields on an incident.

        `fields` should already carry the real field names to write (e.g.
        including the application scope prefix for AI fields - see
        config.settings.ai_field_prefix). This method is intentionally
        generic; it doesn't know or care which fields it's writing, so it
        can be reused for AI-field writeback, work notes, or anything else
        the Table API accepts on PATCH.
        """
        url = f"{self.base_url}/api/now/table/incident/{sys_id}"
        async with httpx.AsyncClient(
            auth=self.auth,
            timeout=self.timeout,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        ) as client:
            response = await client.patch(url, json=fields)
            response.raise_for_status()
            data = response.json()
        return data.get("result", {})

    async def add_work_note(self, sys_id: str, note_text: str) -> dict[str, Any]:
        """
        Append an internal work note to an incident.

        ServiceNow's `work_notes` field is a journal field: PATCHing a value
        to it appends a new journal entry server-side rather than
        overwriting history, so this is safe to call repeatedly.
        """
        return await self.update_incident(sys_id, {"work_notes": note_text})

    async def get_published_articles(
        self, limit: int = 40
    ) -> list[dict[str, Any]]:

        url = f"{self.base_url}/api/now/table/kb_knowledge"

        params = {
            "sysparm_limit": limit,
            "sysparm_display_value": "true",
            "sysparm_fields": (
                "sys_id,number,short_description,"
                "text,workflow_state,published,version,"
                "kb_knowledge_base,kb_category,x_2215387_sprint_0_service"
            ),
            "sysparm_query": (
               f"workflow_state=published^"
               "revised_by=f1654ec4839f0f1058aef1d6feaad30e^"
                f"kb_knowledge_base="
                f"{settings.servicenow_knowledge_base_sys_id}"
                f"^ORDERBYDESCsys_updated_on"
            ),
        }

        async with httpx.AsyncClient(
            auth=self.auth,
            timeout=self.timeout,
            headers={"Accept": "application/json"},
        ) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            print("ServiceNow authentication: SUCCESS")

            data = response.json()

        return data.get("result", [])