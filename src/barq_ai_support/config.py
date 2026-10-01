from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    servicenow_instance_url: str
    servicenow_username: str
    servicenow_password: str
    servicenow_knowledge_base_sys_id: str

    # --- Gemini / Embedding settings (S2.2) ---
    gemini_api_key: str = ""

    # --- Qdrant / vector storage settings (S2.2) ---
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection_name: str = "barq_kb_chunks"

    # --- Retrieval settings (S2.3) ---
    retrieval_score_threshold: float = 0.4
    retrieval_top_k: int = 5

    # --- Tracing settings (S3.6) ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # --- Agent LLM settings (S3.4) ---
    # The Sprints-provided LiteLLM proxy gives free access to a Gemini model
    # through an OpenAI-compatible endpoint (env var name matches the
    # platform's own convention). LLM_MODEL must include the "gemini/"
    # provider prefix for the proxy to route correctly.
    litellm_base_url: str = ""
    openai_api_key: str = ""
    llm_model: str = "gemini/gemini-3.5-flash-lite"
    llm_temperature: float = 0.0

    # --- S3.4 worker settings ---
    # The application scope prefix for the AI fields on the incident table
    # (ai_status, ai_confidence, ai_suggested_response, human_review_required,
    # ai_processed). Configurable because the exact scope differs per
    # ServiceNow PDI - set this in .env, not here.
    ai_field_prefix: str = "x_2066139_ai_triag_"
    # Max ReAct loop iterations before the fail-safe escalation fires.
    agent_max_steps: int = 6
    # searchKB only shows the agent chunks scoring above this.
    agent_chunk_threshold: float = 0.65
    # searchKB calls allowed before the run escalates to a human.
    agent_max_searches: int = 3

    # --- S3.3: HMAC signing secrets ---
    # Authoritative auth mechanism for both event receivers below -- there
    # is no separate shared-secret header contract anymore.
    incident_signing_secret: str = ""
    kb_signing_secret: str = ""

    # --- S3.3 / S3.6: Celery + Redis ---
    # celery_broker_url and redis_url point at the same instance but serve
    # different roles: celery_broker_url is Celery's own task queue
    # connection; redis_url is what the incident/KB receivers use directly
    # for SETNX-based event dedup. Kept as separate settings even though
    # they're typically the same URL, since they're conceptually distinct
    # and may need to diverge (e.g. different Redis instances in prod).
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    redis_url: str = "redis://localhost:6379/0"
    dedup_key_prefix: str = "evt:"
    dedup_ttl_seconds: int = 24 * 60 * 60

    # --- S3.3: SQL state store (KB sync) ---
    kb_state_db_url: str = "sqlite:///./kb_state.db"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
