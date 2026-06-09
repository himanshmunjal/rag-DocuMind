from pydantic_settings import BaseSettings
from pathlib import Path

class Settings(BaseSettings):
    GEMINI_API_KEY: str

    BASE_DIR: Path = Path(__file__).parent.parent
    UPLOAD_DIR: Path = BASE_DIR / "data" / "uploads"
    CHROMA_DIR: Path = BASE_DIR / "data" / "chroma_db"

    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 64
    SEMANTIC_CHUNKING: bool = True

    SEMANTIC_OVERLAP: bool = True

    TOP_K: int = 6
    RERANK_TOP_N: int = 3
    SIMILARITY_THRESHOLD: float = 0.35

    RERANK_MODEL: str = "cross-encoder/ms-marco-MiniLM-L6-v2"

    LLM_MODEL: str = "models/gemini-2.5-flash"
    EMBEDDING_MODEL: str = "models/gemini-embedding-001"

    MAX_TOKENS: int = 1024
    TEMPERATURE: float = 0.0

    CONFIDENCE_THRESHOLD: float = 0.4

    LOW_CONFIDENCE_RESPONSE: str = (
        "I don't have enough information in the uploaded documents to answer this confidently."
    )

    class Config():
        env_file = ".env"
        encoding = "UTF-8"

settings = Settings()

settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
settings.CHROMA_DIR.mkdir(parents=True, exist_ok=True)