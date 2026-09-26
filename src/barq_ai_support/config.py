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
        # --- Retrieval settings (S2.3) ---
    retrieval_score_threshold: float = 0.75
    retrieval_top_k: int = 5
    
        # --- Celery / Redis settings (S3.6) ---
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # --- Tracing settings (S3.6) ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # --- Agent LLM settings (S3.4) ---
    # The Sprints-provided LiteLLM proxy gives free access to a Gemini model
    # through an OpenAI-compatible endpoint (env var name matches the
    # platform's own convention).
    litellm_base_url: str = ""
    openai_api_key: str = ""
    llm_model: str = "gemini-3.5-flash"
    llm_temperature: float = 0.0

    # --- S3.4 worker settings ---
    # The application scope prefix for the AI fields on the incident table
    # (ai_status, ai_confidence, ai_suggested_response, human_review_required,
    # ai_processed). Configurable because the exact scope name is owned by
    # S3.1 and may not be finalized yet - update this once confirmed, no
    # code changes needed elsewhere.
    ai_field_prefix: str = "x_2066139_ai_triag_"
    # Max ReAct loop iterations before the fail-safe escalation fires.
    agent_max_steps: int = 6


    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()