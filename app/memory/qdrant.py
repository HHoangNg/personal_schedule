from typing import Any


class QdrantMemory:
    """Thin adapter; embeddings are deliberately injected to keep provider choice explicit."""

    def __init__(
        self,
        url: str,
        collection: str,
        vector_name: str = "dense",
        api_key: str | None = None,
    ):
        from qdrant_client import QdrantClient

        self.client = QdrantClient(url=url, api_key=api_key)
        self.collection = collection
        self.vector_name = vector_name

    def ensure_collection(self, vector_size: int = 768) -> None:
        """Create the collection once; schedule JSON remains the source of truth."""
        from qdrant_client.models import Distance, VectorParams

        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config={
                    self.vector_name: VectorParams(size=vector_size, distance=Distance.COSINE)
                },
            )

    def upsert(self, point_id: str, vector: list[float], payload: dict[str, Any]) -> None:
        from qdrant_client.models import PointStruct

        self.client.upsert(
            self.collection,
            [PointStruct(id=point_id, vector={self.vector_name: vector}, payload=payload)],
        )

    def search(self, vector: list[float], limit: int = 5):
        return self.client.query_points(
            collection_name=self.collection,
            query=(self.vector_name, vector),
            limit=limit,
        ).points

    def delete_by_user(self, user_id: str) -> None:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        self.client.delete(
            collection_name=self.collection,
            points_selector=Filter(
                must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]
            ),
        )
