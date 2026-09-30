from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.v1.auth import router as auth_router
from app.api.v1.conversations import router as conversations_router
from app.api.v1.runs import router as runs_router

app = FastAPI(title="Agent Containment Control Plane API")

app.include_router(auth_router)
app.include_router(conversations_router)
app.include_router(runs_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Static UI (Option A): plain HTML/CSS/JS served same-origin by this same
# FastAPI app -- no CORS to configure, no separate frontend container, no
# Node/build toolchain. Mounted last, after every API route above, so it
# never shadows /api/v1/* or /healthz; StaticFiles(html=True) serves
# static/index.html for "/" and lets the page's own JS (static/app.js)
# drive everything else via same-origin fetch() calls.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
