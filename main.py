from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, BackgroundTasks
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI()


@app.exception_handler(RequestValidationError)
async def on_validation_error(request, exc):
    body = await request.body()
    print("VALIDATION FAILED")
    print("Raw body received:", body)
    print("Errors:", exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


class IncidentEvent(BaseModel):
    incident_sys_id: str
    number: str
    short_description: str
    description: str | None = ""


@app.post("/webhook", status_code=202)
async def webhook(payload: IncidentEvent, background_tasks: BackgroundTasks):
    print(f"Received event for {payload.number} (sys_id: {payload.incident_sys_id})")
    background_tasks.add_task(handle_event, payload)
    return {"status": "accepted", "number": payload.number}


def handle_event(payload: IncidentEvent):
    # Sprint 1 scope ends here: just prove the event was received.
    # AI processing, retrieval, and write-back belong to later sprints.
    print(f"Background task ran for {payload.number}: {payload.short_description}")