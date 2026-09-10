import hmac
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="AI ServiceNow Support Assistant")

EXPECTED_SECRET = os.getenv("SERVICENOW_WEBHOOK_SECRET")
if not EXPECTED_SECRET:
    raise RuntimeError("SERVICENOW_WEBHOOK_SECRET missing. Set it in .env.")


@app.exception_handler(RequestValidationError)
async def on_validation_error(request, exc):
    body = await request.body()
    print("VALIDATION FAILED")
    print("Raw body received:", body.decode("utf-8", errors="ignore"))
    print("Errors:", exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


class IncidentEvent(BaseModel):
    incident_sys_id: str = Field(..., description="ServiceNow 32-character sys_id")
    number: str = Field(..., description="Incident number (e.g., INC0010001)")
    short_description: str = Field(..., description="Short description")
    description: str | None = Field(default="", description="Detailed description")


def handle_event(payload: IncidentEvent):
    # Sprint 1 scope ends here: just prove the event was received.
    # AI processing, retrieval, and write-back belong to later sprints.
    print(f"Background task ran for {payload.number}: {payload.short_description}")
    print(f"Details -> Sys ID: {payload.incident_sys_id}")


@app.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def webhook(
    payload: IncidentEvent,
    background_tasks: BackgroundTasks,
    x_servicenow_secret: str | None = Header(default=None),
):
    if not x_servicenow_secret or not hmac.compare_digest(
        x_servicenow_secret, EXPECTED_SECRET
    ):
        print(f"UNAUTHORIZED ATTEMPT: Received header '{x_servicenow_secret}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-ServiceNow-Secret header.",
        )

    print(f"Received event for {payload.number} (sys_id: {payload.incident_sys_id})")

    background_tasks.add_task(handle_event, payload)

    return {
        "status": "accepted",
        "number": payload.number,
        "message": "Incident received and queued for processing.",
    }
