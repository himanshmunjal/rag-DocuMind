# DocuMind

DocuMind is a retrieval-augmented question-answering app for SEC filings (10-K / 10-Q). Search for a company, ingest a filing from EDGAR, and ask questions about it. Every answer comes with source citations and a confidence level.

## How it works

Each question is routed one of three ways (`backend/core/ingestion/query_router.py`):

| Route | Example | How it is answered |
|---|---|---|
| **numeric** | "What was Apple's revenue in FY2024?" | A fact lookup in the XBRL-backed SQLite store. No LLM is involved. |
| **computed** | "How did gross margin change YoY?" | Facts are combined with arithmetic in Python, and the LLM only narrates the result. |
| **qualitative** | "What risks does Apple flag?" | Retrieve, then rerank, then the hallucination guard, then generate with citations. |

The pipeline has these parts:

- **Ingestion**: EDGAR client, filing chunker (split by Item), PDF/OCR loaders, sentence-transformer embeddings (`all-MiniLM-L6-v2`), and a ChromaDB vector store.
- **Retrieval**: vector search, then cross-encoder reranking (`ms-marco-MiniLM-L-12-v2`).
- **Hallucination guard**: refuses to answer when the reranker and cosine-similarity signals both fall below their thresholds. Otherwise it attaches a HIGH / MEDIUM / LOW confidence level.
- **Generation**: Groq (default) or Google Gemini, chosen with `LLM_PROVIDER`.
- **Multi-filing comparison**: compares filings across companies, both numerically and qualitatively.

## Project structure

```
backend/
  main.py            FastAPI entry point
  config.py          Settings (loaded from backend/.env)
  api/               Routes and Pydantic schemas
  core/
    ingestion/       EDGAR, chunking, embedding, indexing, structured store, query router
    retrieval/       Searcher and reranker
    hallucination/   Confidence guard
    generation/      LLM client, generator, citation builder
    multi_doc.py     Cross-filing comparison
  eval/              Regression eval set and runner
  scripts/           Diagnostics and maintenance (reindex, rebuild facts)
DocuMind/            React 19 + Vite + Tailwind frontend
archived/            Old route implementation, kept for reference
data/                Runtime data: ChromaDB, filings.db, ticker cache (git-ignored)
```

## Setup

### Backend

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp backend/.env.example backend/.env   # add GROQ_API_KEY and/or GEMINI_API_KEY
cd backend
uvicorn main:app --reload --port 8000
```

API docs are served at http://localhost:8000/docs.

### Frontend

```bash
cd DocuMind
npm install
npm run dev
```

The app runs at http://localhost:5173. By default it calls `http://localhost:8000/api/v1`. Set `VITE_API_BASE_URL` to point it somewhere else.

## API

All endpoints are under `/api/v1`:

| Method | Path | Purpose |
|---|---|---|
| GET | `/companies/search` | Search EDGAR companies by name or ticker |
| GET | `/companies/{cik}/filings` | List a company's filings |
| POST | `/filings/ingest` | Download, chunk and index a filing |
| POST | `/query` | Ask a question |
| POST | `/filings/compare` | Compare filings across companies |
| GET | `/documents` | List indexed documents |
| DELETE | `/documents/{source}` | Remove a document from the index |

## Evaluation

With the API running:

```bash
python -m backend.eval.run_eval
python -m backend.eval.run_eval --only numeric
```

This grades the numeric, computed, qualitative and refusal cases in `backend/eval/eval_set.json` and writes the results to `eval_results.json`.

## Notes

- The SEC requires a descriptive User-Agent for EDGAR requests. Set it with `EDGAR_USER_AGENT` in `config.py`.
- Free-tier LLM keys are rate-limited. Per-minute limits are waited out automatically, but a per-day quota will return a 503.
