from typing import Annotated

from fastapi import APIRouter, Depends

from api.deps import get_store
from api.schemas import EvalRun
from api.store import Store

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("", response_model=list[EvalRun])
def latest_metrics(store: Annotated[Store, Depends(get_store)]) -> list[EvalRun]:
    """Latest eval run per (dataset, split), exactly as eval/ wrote it. Never hard-coded."""
    return store.latest_metrics()
