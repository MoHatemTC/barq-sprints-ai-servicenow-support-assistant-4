import argparse
import asyncio

from barq_ai_support.servicenow_client import ServiceNowClient
from barq_ai_support.embeddings import (
    embedding_dim,
    embedding_model_name,
    get_embedding_fn,
)
from barq_ai_support.ingestion.chunker import chunk_articles
from barq_ai_support.ingestion.embed_and_store import ingest_chunks_to_qdrant
from barq_ai_support.retrieval.retriever import retrieve


async def main(use_real: bool = False):
    embedding_fn = get_embedding_fn(use_real=True if use_real else False)
    dim = embedding_dim(embedding_fn)
    print(f"Embedding: {embedding_model_name(embedding_fn)} ({dim}-dim)")

    print("1. Fetching published KB articles from ServiceNow...")
    client = ServiceNowClient()
    articles = await client.get_published_articles()
    print(f"   -> Articles retrieved: {len(articles)}")

    if not articles:
        print("No articles retrieved. Exiting.")
        return

    print("\n2. Chunking articles with BeautifulSoup section parser...")
    chunks = chunk_articles(
        articles,
        chunk_size=400,
        overlap=50,
    )
    print(f"   -> Chunks generated: {len(chunks)}")

    print(f"\n3. Ingesting chunks into Qdrant Cloud ('kb_chunks', {dim}-dim)...")
    count = ingest_chunks_to_qdrant(
        chunks,
        embedding_fn=embedding_fn,
        recreate_collection=use_real,
        vector_size=dim,
    )
    print(f"   -> Successfully ingested {count} chunks into Qdrant.")

    print("\n4. Validating retrieval against Qdrant collection...")
    # Test query using the first article's short description
    test_query = articles[0].get("short_description", "VPN connection error")
    print(f"   Testing search query: '{test_query}'")

    result = retrieve(
        query=test_query, score_threshold=0.0, embedding_fn=embedding_fn
    )
    print(f"   Retrieval result OK: {result.ok}")
    print(f"   Number of matched chunks retrieved: {len(result.chunks)}")
    if result.chunks:
        top = result.chunks[0]
        print(f"   Top Match -> Article Number: {top.article_number} | Score: {top.score:.4f}")
        print(f"   Snippet: {top.text[:150]}...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest ServiceNow KB into Qdrant")
    parser.add_argument(
        "--real",
        action="store_true",
        help="Use real Gemini embeddings (768-dim, recreates collection). "
        "Default is the offline stub (384-dim).",
    )
    args = parser.parse_args()
    asyncio.run(main(use_real=args.real))
