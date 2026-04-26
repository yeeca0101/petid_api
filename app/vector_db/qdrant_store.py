from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchHit:
    point_id: str
    score: float
    payload: Dict[str, Any]


@dataclass(frozen=True)
class PointRecord:
    point_id: str
    vector: Optional[List[float]]
    payload: Dict[str, Any]


class QdrantStore:
    def __init__(
        self,
        url: str,
        api_key: Optional[str],
        collection: str,
        timeout_s: float = 5.0,
    ):
        self.collection = collection
        self.client = QdrantClient(url=url, api_key=api_key, timeout=timeout_s)

    def ensure_collection(self, vector_size: int) -> None:
        """Create the collection if it doesn't exist."""
        try:
            self.client.get_collection(self.collection)
            return
        except Exception:
            pass

        logger.info(
            "Creating Qdrant collection %s | size=%s | distance=Cosine",
            self.collection,
            vector_size,
        )
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=qm.VectorParams(size=int(vector_size), distance=qm.Distance.COSINE),
        )

        # Helpful payload indexes for filtering
        for field_name, field_schema in (
            ("daycare_id", qm.PayloadSchemaType.KEYWORD),
            ("image_id", qm.PayloadSchemaType.KEYWORD),
            ("pet_id", qm.PayloadSchemaType.KEYWORD),
            ("captured_at_ts", qm.PayloadSchemaType.INTEGER),
        ):
            try:
                self.client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field_name,
                    field_schema=field_schema,
                )
            except Exception:
                logger.warning(
                    "Payload index creation failed (non-fatal) | collection=%s | field=%s",
                    self.collection,
                    field_name,
                    exc_info=False,
                )

    def upsert(
        self,
        points: List[qm.PointStruct],
        wait: bool = True,
    ) -> None:
        if not points:
            return
        self.client.upsert(collection_name=self.collection, points=points, wait=wait)

    @staticmethod
    def _normalize_point_id(pid: str) -> str:
        """Qdrant point IDs must be UUID or unsigned int. We standardize to UUID."""
        if pid.startswith("ins_"):
            pid = pid[4:]
        try:
            return str(uuid.UUID(pid))
        except Exception as exc:
            raise ValueError(f"Invalid instance_id: {pid}") from exc

    @staticmethod
    def _external_point_id(pid: str) -> str:
        if pid.startswith("ins_"):
            return pid
        try:
            uuid.UUID(pid)
            return f"ins_{pid}"
        except Exception:
            return pid

    def normalize_instance_ids(self, instance_ids: Iterable[str]) -> List[str]:
        return [self._normalize_point_id(str(i)) for i in instance_ids]

    def external_instance_id(self, pid: str) -> str:
        return self._external_point_id(str(pid))

    def retrieve_vectors(self, instance_ids: Iterable[str]) -> Dict[str, List[float]]:
        ids = self.normalize_instance_ids(instance_ids)
        if not ids:
            return {}
        pts = self.client.retrieve(
            collection_name=self.collection,
            ids=ids,
            with_vectors=True,
            with_payload=False,
        )
        out: Dict[str, List[float]] = {}
        for p in pts:
            if p.vector is None:
                continue
            out[self.external_instance_id(str(p.id))] = list(p.vector)  # type: ignore[arg-type]
        return out

    def retrieve_points(
        self,
        instance_ids: Iterable[str],
        with_vectors: bool = False,
    ) -> Dict[str, PointRecord]:
        ids = self.normalize_instance_ids(instance_ids)
        if not ids:
            return {}
        pts = self.client.retrieve(
            collection_name=self.collection,
            ids=ids,
            with_vectors=with_vectors,
            with_payload=True,
        )
        out: Dict[str, PointRecord] = {}
        for p in pts:
            external_id = self.external_instance_id(str(p.id))
            vec = None
            if with_vectors and p.vector is not None:
                vec = list(p.vector)  # type: ignore[arg-type]
            out[external_id] = PointRecord(point_id=str(p.id), vector=vec, payload=p.payload or {})
        return out

    def set_payload(self, instance_ids: Iterable[str], payload: Dict[str, Any]) -> None:
        ids = self.normalize_instance_ids(instance_ids)
        if not ids:
            return
        self.client.set_payload(
            collection_name=self.collection,
            payload=payload,
            points=ids,
        )

    def delete_points(self, instance_ids: Iterable[str], wait: bool = True) -> None:
        ids = self.normalize_instance_ids(instance_ids)
        if not ids:
            return
        self.client.delete(
            collection_name=self.collection,
            points_selector=qm.PointIdsList(points=ids),
            wait=wait,
        )

    def search(
        self,
        vector: List[float],
        limit: int,
        query_filter: Optional[qm.Filter] = None,
    ) -> List[SearchHit]:
        res = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=query_filter,
            limit=int(limit),
            with_payload=True,
            with_vectors=False,
        )
        hits: List[SearchHit] = []
        for r in res.points:
            hits.append(SearchHit(point_id=str(r.id), score=float(r.score), payload=r.payload or {}))
        return hits

    def scroll_points(
        self,
        query_filter: Optional[qm.Filter] = None,
        limit: int = 1000,
        with_vectors: bool = False,
    ) -> List[PointRecord]:
        out: List[PointRecord] = []
        next_offset = None
        while True:
            points, next_offset = self.client.scroll(
                collection_name=self.collection,
                scroll_filter=query_filter,
                limit=int(limit),
                with_payload=True,
                with_vectors=with_vectors,
                offset=next_offset,
            )
            for p in points:
                vec = None
                if p.vector is not None:
                    vec = list(p.vector)  # type: ignore[arg-type]
                out.append(PointRecord(point_id=str(p.id), vector=vec, payload=p.payload or {}))
            if next_offset is None:
                break
        return out


def build_filter(
    daycare_id: Optional[str] = None,
    species: Optional[str] = None,
    captured_from_ts: Optional[int] = None,
    captured_to_ts: Optional[int] = None,
) -> qm.Filter:
    must: List[qm.FieldCondition] = []

    if species:
        must.append(qm.FieldCondition(key="species", match=qm.MatchValue(value=species)))

    if captured_from_ts is not None or captured_to_ts is not None:
        must.append(
            qm.FieldCondition(
                key="captured_at_ts",
                range=qm.Range(
                    gte=captured_from_ts,
                    lt=captured_to_ts,
                ),
            )
        )
    return qm.Filter(must=must)
