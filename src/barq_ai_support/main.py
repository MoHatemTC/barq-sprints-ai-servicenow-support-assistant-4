import httpx

from fastapi import FastAPI, HTTPException, Query

from .servicenow_client import ServiceNowClient


app = FastAPI(
    title="BARQ AI ServiceNow Support Assistant",
    version="1.0.0",
)


client = ServiceNowClient()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/kb/articles")
async def get_articles(
    limit: int = Query(default=40, ge=1, le=100)
):
    try:
        articles = await client.get_published_articles(limit=limit)

        return {
            "count": len(articles),
            "articles": articles,
        }

    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"ServiceNow returned HTTP {exc.response.status_code}",
        ) from exc

    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to ServiceNow",
        ) from exc