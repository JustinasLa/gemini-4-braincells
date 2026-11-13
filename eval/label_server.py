"""Labelling web page for a frozen sample (eval/labels.py). Run with `python -m eval labels serve`.

Only each item's `evidence` block is ever sent to the browser: the detector's status, score and the
cluster id stay on the server. Other labellers' labels are only shown on the adjudication view,
and only for items every labeller of the pair has labelled and that they disagree on.
"""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from eval import labels as L

PAGE = Path(__file__).with_name("label_page.html")


class LabelIn(BaseModel):
    item: int
    labeller: str
    label: str
    note: str = Field(default="", max_length=2000)
    seconds: float | None = None


class AdjudicateIn(LabelIn):
    pass


def newest_sample(root: Path) -> str:
    files = sorted((root / "samples").glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit(f"no samples under {root / 'samples'}; run `python -m eval labels sample`")
    return files[-1].stem


def create_app(name: str | None = None, root: Path | None = None) -> FastAPI:
    root = root or L.labels_dir()
    name = name or newest_sample(root)
    sample = json.loads(L.sample_path(name, root).read_text())
    items = {i["item"]: i for i in sample["items"]}
    lpath = L.labels_path(name, root)
    app = FastAPI(title="Shill or Chill: hand labels", docs_url=None, redoc_url=None)

    def _name(raw: str) -> str:
        try:
            return L.clean_name(raw)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    def _item(k: int) -> dict:
        if k not in items:
            raise HTTPException(404, "no such item")
        return items[k]

    @app.get("/")
    def page() -> FileResponse:
        return FileResponse(PAGE, media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/api/sample")
    def info() -> dict:
        return {"name": name, "n_items": len(items), "created_at": sample["created_at"]}

    @app.get("/api/progress")
    def progress(labeller: str) -> dict:
        who = _name(labeller)
        labs, adj = L.load_records(lpath)
        mine = labs.get(who, {})
        return {
            "labeller": who,
            "n_items": len(items),
            "done": {str(k): r["label"] for k, r in sorted(mine.items()) if k in items},
            "labellers": {n: len(v.keys() & items.keys()) for n, v in sorted(labs.items())},
            "disagreements": len(_disagreements(labs, adj)),
        }

    @app.get("/api/item/{k}")
    def item(k: int, labeller: str) -> dict:
        who = _name(labeller)
        it = _item(k)
        labs, _ = L.load_records(lpath)
        mine = labs.get(who, {}).get(k)
        return {
            "item": k,
            "n_items": len(items),
            "evidence": it["evidence"],  # never it["detector"]
            "mine": {"label": mine["label"], "note": mine.get("note", "")} if mine else None,
        }

    @app.post("/api/label")
    def label(body: LabelIn) -> dict:
        who = _name(body.labeller)
        _item(body.item)
        if body.label not in L.LABELS:
            raise HTTPException(400, f"label must be one of {L.LABELS}")
        L.append_record(
            lpath,
            {
                "type": "label",
                "sample": name,
                "item": body.item,
                "labeller": who,
                "label": body.label,
                "note": body.note.strip(),
                "seconds": body.seconds,
            },
        )
        return {"ok": True}

    def _disagreements(labs: dict, adj: dict) -> list[int]:
        pair = L.pick_pair(labs)
        if not pair:
            return []
        a, b = pair
        common = labs[a].keys() & labs[b].keys() & items.keys()
        return sorted(
            k for k in common if labs[a][k]["label"] != labs[b][k]["label"] and k not in adj
        )

    @app.get("/api/disagreements")
    def disagreements() -> dict:
        labs, adj = L.load_records(lpath)
        out = []
        for k in _disagreements(labs, adj):
            out.append(
                {
                    "item": k,
                    "labels": [
                        {"labeller": n, "label": v[k]["label"], "note": v[k].get("note", "")}
                        for n, v in sorted(labs.items())
                        if k in v
                    ],
                }
            )
        return {"items": out, "adjudicated": len(adj)}

    @app.post("/api/adjudicate")
    def adjudicate(body: AdjudicateIn) -> dict:
        who = _name(body.labeller)
        _item(body.item)
        if body.label not in L.LABELS:
            raise HTTPException(400, f"label must be one of {L.LABELS}")
        L.append_record(
            lpath,
            {
                "type": "adjudication",
                "sample": name,
                "item": body.item,
                "labeller": who,
                "label": body.label,
                "note": body.note.strip(),
            },
        )
        return {"ok": True}

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "sample": name}

    return app
