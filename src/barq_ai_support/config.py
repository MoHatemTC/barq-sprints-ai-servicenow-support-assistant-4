from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    servicenow_instance_url: str
    servicenow_username: str
    servicenow_password: str
    servicenow_knowledge_base_sys_id: str

    # Shared secret checked against the
    # "X-ServiceNow-Secret" header.
    servicenow_webhook_secret: str = ""

    # --- Gemini / Embedding settings (S2.2) ---
    gemini_api_key: str = ""

    # --- Qdrant / vector storage settings (S2.2) ---
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection_name: str = "barq_kb_chunks"

    # --- Retrieval settings (S2.3) ---
    retrieval_score_threshold: float = 0.75
    retrieval_top_k: int = 5

    # --- S3.3: HMAC signing secrets ---
    incident_signing_secret: str = ""
    kb_signing_secret: str = ""

    # --- S3.3: Redis / dedup ---
    redis_url: str = "redis://localhost:6379/0"
    dedup_key_prefix: str = "evt:"
    dedup_ttl_seconds: int = 24 * 60 * 60

    # --- S3.3: Celery ---
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/0"

    # --- S3.3: SQL state store (KB sync) ---
    kb_state_db_url: str = "sqlite:///./kb_state.db"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()