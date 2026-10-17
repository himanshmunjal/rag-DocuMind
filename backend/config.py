from pydantic_settings import BaseSettings
from pathlib import Path

class Settings(BaseSettings):
    # Which LLM answers qualitative questions: "groq" or "gemini".
    # Keys live in backend/.env only — never in source.
    LLM_PROVIDER: str = "groq"
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    GEMINI_API_KEY: str = ""

    EDGAR_USER_AGENT: str = "DocuMind/1.0 (Research tool; your-email@example.com)"

    BASE_DIR: Path = Path(__file__).parent.parent
    UPLOAD_DIR: Path = BASE_DIR / "data" / "uploads"
    CHROMA_DIR: Path = BASE_DIR / "data" / "chroma_db"
    FILINGS_DB_PATH: Path = BASE_DIR / "data" / "filings.db"

    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 64
    SEMANTIC_CHUNKING: bool = True

    SEMANTIC_OVERLAP: bool = True

    TOP_K: int = 20
    RERANK_TOP_N: int = 5
    SIMILARITY_THRESHOLD: float = 0.35

    RERANKER_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-12-v2"

    LLM_MODEL: str = "models/gemini-2.5-flash"
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"

    # gemini-2.5-flash "thinks" before answering, and those hidden reasoning
    # tokens count against this limit — at 1024 they consumed nearly all of
    # it and answers came back cut off mid-sentence.
    MAX_TOKENS: int = 8192
    TEMPERATURE: float = 0.0

    CONFIDENCE_THRESHOLD: float = 0.4

    # Hallucination guard (guard.py) — either signal can veto generation.
    # Cross-encoder (sigmoid of ms-marco logits): answerable filing questions
    # score >= ~0.07 at the top even when phrased loosely; off-corpus
    # questions (earnings calls, analyst notes) score ~0.001.
    RERANK_REFUSAL_THRESHOLD: float = 0.01
    # Raw cosine similarity (not the [0,1]-rescaled bi_encoder_score) of the
    # best chunk. all-MiniLM-L6-v2 puts on-topic filing text at >= ~0.4.
    COSINE_REFUSAL_THRESHOLD: float = 0.30
    # A chunk is dropped from the context only when BOTH signals reject it:
    # cross-encoder below RERANK_CHUNK_FLOOR AND raw cosine more than
    # COSINE_KEEP_MARGIN below the best chunk's. The cross-encoder alone
    # scores ~0 for on-topic passages whose wording differs from the question
    # (Apple's "Department of Justice Lawsuit" for "what legal proceedings…").
    # The top chunk is always kept.
    RERANK_CHUNK_FLOOR: float = 0.001
    COSINE_KEEP_MARGIN: float = 0.10

    LOW_CONFIDENCE_RESPONSE: str = (
        "I don't have enough information in the uploaded documents to answer this confidently."
    )

    PREFERRED_FORM_TYPES: list[str] = ["10-K", "10-Q"]

    METRIC_TAG_ALIASES: dict[str, list[str]] = {
        "revenue": [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
        ],
        "cost_of_revenue": [
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
            "CostOfGoodsSold",
        ],
        "gross_profit": [
            "GrossProfit",
        ],
        "operating_income": [
            "OperatingIncomeLoss",
        ],
        "operating_expenses": [
            "OperatingExpenses",
            "CostsAndExpenses",
        ],
        "rd_expense": [
            "ResearchAndDevelopmentExpense",
        ],
        "net_income": [
            "NetIncomeLoss",
            "ProfitLoss",
        ],
        "eps_basic": [
            "EarningsPerShareBasic",
        ],
        "eps_diluted": [
            "EarningsPerShareDiluted",
        ],
        "total_assets": [
            "Assets",
        ],
        "total_liabilities": [
            "Liabilities",
        ],
        "stockholders_equity": [
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ],
        "cash_and_equivalents": [
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ],
        "long_term_debt": [
            "LongTermDebtNoncurrent",
            "LongTermDebt",
        ],
        "shares_outstanding": [
            "EntityCommonStockSharesOutstanding",  # lives under "dei", not "us-gaap"
        ],
    }

    # Metrics whose tag lives under the "dei" taxonomy instead of "us-gaap"
    DEI_METRICS: set[str] = {"shares_outstanding"}

    class Config():
        # absolute, so the key loads no matter which directory the server starts from
        env_file = Path(__file__).parent / ".env"
        encoding = "UTF-8"

settings = Settings()

settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
settings.CHROMA_DIR.mkdir(parents=True, exist_ok=True)

# from pydantic_settings import BaseSettings
# from pathlib import Path

# class Settings(BaseSettings):
#     GEMINI_API_KEY: str

#     BASE_DIR: Path = Path(__file__).parent.parent
#     UPLOAD_DIR: Path = BASE_DIR / "data" / "uploads"
#     CHROMA_DIR: Path = BASE_DIR / "data" / "chroma_db"

#     CHUNK_SIZE: int = 512
#     CHUNK_OVERLAP: int = 64
#     SEMANTIC_CHUNKING: bool = True

#     SEMANTIC_OVERLAP: bool = True

#     TOP_K: int = 6
#     RERANK_TOP_N: int = 3
#     SIMILARITY_THRESHOLD: float = 0.35

#     RERANK_MODEL: str = "cross-encoder/ms-marco-MiniLM-L6-v2"

#     LLM_MODEL: str = "models/gemini-2.5-flash"
#     EMBEDDING_MODEL: str = "models/gemini-embedding-001"

#     MAX_TOKENS: int = 1024
#     TEMPERATURE: float = 0.0

#     CONFIDENCE_THRESHOLD: float = 0.4

#     LOW_CONFIDENCE_RESPONSE: str = (
#         "I don't have enough information in the uploaded documents to answer this confidently."
#     )

#     class Config():
#         env_file = ".env"
#         encoding = "UTF-8"

# settings = Settings()

# settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
# settings.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
