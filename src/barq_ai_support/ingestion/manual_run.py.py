import asyncio

from barq_ai_support.servicenow_client import ServiceNowClient
from barq_ai_support.ingestion.chunker import chunk_articles


async def main():
    client = ServiceNowClient()

    articles = await client.get_published_articles()

    print(f"Articles retrieved: {len(articles)}")

    if not articles:
        print("No articles were retrieved.")
        return

    chunks = chunk_articles(
        articles,
        chunk_size=400,
        overlap=50,
    )

    print(f"Chunks generated: {len(chunks)}")

    for chunk in chunks:
        print("\n" + "=" * 80)

        print("Text:")
        print(chunk["text"])

        print("\nMetadata:")
        print(chunk["metadata"])


if __name__ == "__main__":
    asyncio.run(main())