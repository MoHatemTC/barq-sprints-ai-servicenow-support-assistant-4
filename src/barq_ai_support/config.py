from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    servicenow_instance_url: str
    servicenow_username: str
    servicenow_password: str
    servicenow_knowledge_base_sys_id: str
    # Shared secret checked against the "X-ServiceNow-Secret" header the
    # Business Rule sends (see businessRule/business_rule.js).
    servicenow_webhook_secret: str = ""

    # --- Qdrant / retrieval settings (S2.3) ---
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection_name: str = "kb_chunks"
    retrieval_score_threshold: float = 0.75
    retrieval_top_k: int = 5

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()