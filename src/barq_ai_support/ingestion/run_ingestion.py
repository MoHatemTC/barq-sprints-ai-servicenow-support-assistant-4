"""
S2.2 - manual entry point.

Run with:  python -m barq_ai_support.ingestion.run_ingestion

Pulls published articles from ServiceNow, chunks them (S2.1), embeds
and stores them in Qdrant (S2.2). Safe to re-run - deterministic point
IDs mean re-ingesting the same articles overwrites existing points
rather than duplicating them.
"""

import asyncio

from ..servicenow_client import ServiceNowClient
from .chunker import chunk_articles
from .embed_and_store import embed_and_store


async def main():
    client = ServiceNowClient()
    articles = await client.get_published_articles()
    print(f"Articles retrieved: {len(articles)}")

    if not articles:
        print("No articles were retrieved. Nothing to ingest.")
        return

    chunks = chunk_articles(articles, chunk_size=400, overlap=50)
    print(f"Chunks generated: {len(chunks)}")

    summary = embed_and_store(chunks)
    print(f"Ingestion complete: {summary}")


if __name__ == "__main__":
    asyncio.run(main())
