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

    async def get_published_articles(
        self, limit: int = 40
    ) -> list[dict[str, Any]]:

        url = f"{self.base_url}/api/now/table/kb_knowledge"

        params = {
            "sysparm_limit": limit,
            "sysparm_display_value": "true",
            "sysparm_fields": (
                "sys_id,number,short_description,"
                "text,workflow_state,published,"
                "kb_knowledge_base,kb_category,x_2215387_sprint_0_service"
            ),
            "sysparm_query": (
               f"workflow_state=published^"
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