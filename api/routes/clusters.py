from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_store
from api.schemas import ClusterDetail, ClusterSummary, Status
from api.store import Store

router = APIRouter(prefix="/api/clusters", tags=["clusters"])


@router.get("", response_model=list[ClusterSummary])
def list_clusters(
    store: Annotated[Store, Depends(get_store)],
    status: Status | None = None,
    min_score: Annotated[float, Query(ge=0, le=1)] = 0.0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    include_synthetic: bool = True,
) -> list[ClusterSummary]:
    """Newest clusters first. `status` is the detector's call; `verdict` is the analyst's."""
    return store.list_clusters(
        status=status, min_score=min_score, limit=limit, include_synthetic=include_synthetic
    )


@router.get("/{cluster_id}", response_model=ClusterDetail)
def get_cluster(cluster_id: int, store: Annotated[Store, Depends(get_store)]) -> ClusterDetail:
    cluster = store.get_cluster(cluster_id)
    if cluster is None:
        raise HTTPException(404, "cluster not found")
    return cluster
