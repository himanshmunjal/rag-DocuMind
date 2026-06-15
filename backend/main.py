"""
main.py — FastAPI application entry point.

Run locally:
    uvicorn main:app --reload --port 8000

Interactive docs available at:
    http://localhost:8000/docs   (Swagger UI)
    http://localhost:8000/redoc  (ReDoc)
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from config import settings
from api.routes import router


# ── Lifespan: startup / shutdown ───────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once on startup (before the first request) and once on shutdown.
    Use this to warm up expensive resources — embedding model, ChromaDB
    connection — so the first real request isn't slow.
    """
    # ── Startup ──
    logger.info("DocuMind API starting up…")
    logger.info(f"  Upload dir : {settings.UPLOAD_DIR}")
    logger.info(f"  ChromaDB   : {settings.CHROMA_DIR}")
    logger.info(f"  LLM model  : {settings.LLM_MODEL}")
    logger.info(f"  Embed model: {settings.EMBEDDING_MODEL}")

    # Eagerly open the ChromaDB collection so connection errors surface at
    # startup, not mid-request.
    try:
        from core.ingestion.indexer import get_collection_stats
        stats = get_collection_stats()
        logger.success(
            f"ChromaDB ready — {stats['total_vectors']} vectors "
            f"across {stats['document_count']} document(s)"
        )
    except Exception as exc:
        # Non-fatal on startup; individual requests will fail with a 503
        logger.error(f"ChromaDB warm-up failed: {exc}")

    yield  # application runs here

    # ── Shutdown ──
    logger.info("DocuMind API shutting down.")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "DocuMind API",
    description = (
        "RAG-powered conversational document intelligence. "
        "Upload PDFs, ask questions, get answers with exact source citations."
    ),
    version     = "0.1.0",
    docs_url    = "/docs",
    redoc_url   = "/redoc",
    lifespan    = lifespan,
)


# ── CORS ───────────────────────────────────────────────────────────────────────
# During development allow the Vite dev server (port 5173) and any localhost
# origin.  Lock this down to your production domain before deploying.

app.add_middleware(
    CORSMiddleware,
    allow_origins     = [
        "http://localhost:5173",   # Vite dev server
        "http://localhost:3000",   # CRA / fallback
        "http://127.0.0.1:5173",
    ],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)


# ── Routes ─────────────────────────────────────────────────────────────────────

app.include_router(router, prefix="/api/v1", tags=["documind"])


# ── Root ───────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    return {
        "service": "DocuMind API",
        "version": "0.1.0",
        "docs":    "/docs",
    }