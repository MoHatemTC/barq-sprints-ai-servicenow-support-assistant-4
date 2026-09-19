import asyncio
import os
import uuid

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from barq_ai_support.ingestion.chunker import chunk_articles
from barq_ai_support.ingestion.embedding import create_embedding
from barq_ai_support.servicenow_client import ServiceNowClient


load_dotenv()


COLLECTION_NAME = "barq_kb_chunks"

# Number of points to save to Qdrant at a time
BATCH_SIZE = 10


client = QdrantClient(
    url=os.environ["QDRANT_URL"],
    api_key=os.environ["QDRANT_API_KEY"],
)


def create_collection():
    """
    Create the Qdrant collection if it does not already exist.
    """

    if client.collection_exists(COLLECTION_NAME):
        print(
            f"Collection '{COLLECTION_NAME}' already exists."
        )
        return

    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(
            size=768,
            distance=Distance.COSINE,
        ),
    )

    print(
        f"Collection '{COLLECTION_NAME}' created."
    )


def generate_point_id(chunk: dict) -> str:
    """
    Generate a deterministic ID for each chunk.

    Same article + same chunk index
    => same point ID
    => repeated ingestion updates the existing point
       instead of creating a duplicate.
    """

    article_id = chunk["metadata"]["sys_id"]
    chunk_index = chunk["metadata"]["chunk_index"]

    value = f"{article_id}:{chunk_index}"

    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            value,
        )
    )


def upsert_chunks(
    chunks: list[dict],
    batch_size: int = BATCH_SIZE,
) -> None:
    """
    Generate embeddings for chunks and store them in Qdrant
    in batches.

    Chunks are saved after every batch so that already
    processed chunks remain stored if the embedding API
    fails later.
    """

    points = []
    total_upserted = 0

    for index, chunk in enumerate(chunks, start=1):

        print(
            f"Embedding chunk {index}/{len(chunks)}..."
        )

        try:
            # Generate embedding from chunk text
            vector = create_embedding(
                chunk["text"]
            )

        except Exception as error:
            print(
                f"\nEmbedding failed at chunk {index}."
            )
            print(
                f"Error: {error}"
            )

            # Save any points waiting in the current batch
            if points:
                client.upsert(
                    collection_name=COLLECTION_NAME,
                    points=points,
                )

                total_upserted += len(points)

                print(
                    f"Saved final batch of {len(points)} chunks."
                )

            print(
                f"Total chunks stored before stopping: "
                f"{total_upserted}"
            )

            print(
                "Run the ingestion again later to retry "
                "the failed chunks."
            )

            return

        # Generate deterministic point ID
        point_id = generate_point_id(
            chunk
        )

        # Create Qdrant point
        point = PointStruct(
            id=point_id,
            vector=vector,
            payload=chunk["metadata"],
        )

        points.append(point)

        # Upsert when batch is full
        if len(points) >= batch_size:

            client.upsert(
                collection_name=COLLECTION_NAME,
                points=points,
            )

            total_upserted += len(points)

            print(
                f"Upserted batch: {len(points)} chunks."
            )

            print(
                f"Total upserted so far: "
                f"{total_upserted}"
            )

            # Clear batch
            points = []

    # Upsert remaining chunks
    if points:

        client.upsert(
            collection_name=COLLECTION_NAME,
            points=points,
        )

        total_upserted += len(points)

        print(
            f"Upserted final batch: {len(points)} chunks."
        )

    print(
        f"Finished. Total upserted: "
        f"{total_upserted} chunks."
    )


def ingest_articles(
    articles: list[dict],
    chunk_size: int,
    overlap: int,
) -> None:
    """
    Complete ingestion pipeline:

    ServiceNow articles
        ↓
    Chunking
        ↓
    Embedding
        ↓
    Qdrant
    """

    print(
        f"Received {len(articles)} articles."
    )

    # Convert articles into chunks
    chunks = chunk_articles(
        articles,
        chunk_size=chunk_size,
        overlap=overlap,
    )

    print(
        f"Generated {len(chunks)} chunks."
    )

    # Generate embeddings and store them in Qdrant
    upsert_chunks(
        chunks,
        batch_size=BATCH_SIZE,
    )


async def main():

    # 1. Create Qdrant collection
    create_collection()

    # 2. Create ServiceNow client
    servicenow_client = ServiceNowClient()

    # 3. Get real published articles from ServiceNow
    print(
        "Fetching published articles from ServiceNow..."
    )

    articles = await servicenow_client.get_published_articles(
        limit=40
    )

    print(
        f"Fetched {len(articles)} published articles."
    )

    if not articles:
        print(
            "No published articles were returned."
        )
        return

    # 4. Chunk + embed + store in Qdrant
    ingest_articles(
        articles,
        chunk_size=1000,
        overlap=100,
    )

    # 5. Check total number of points
    count = client.count(
        collection_name=COLLECTION_NAME,
    )

    print(
        f"Total points in Qdrant: {count.count}"
    )


if __name__ == "__main__":
    asyncio.run(main())