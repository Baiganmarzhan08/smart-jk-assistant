import logging
from math import isfinite, sqrt

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout


EMBEDDING_MODEL = "gemini-embedding-001"
DIMENSIONS = 768
BASE_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}"
LOGGER = logging.getLogger(__name__)


class EmbeddingUnavailable(Exception):
    """Gemini did not return usable embeddings."""

    def __init__(self, status: int | None = None) -> None:
        self.status = status
        super().__init__(f"Gemini HTTP {status}" if status else "Gemini embeddings unavailable")


def normalized_vector(values: list[float]) -> list[float]:
    if len(values) != DIMENSIONS or not all(isfinite(value) for value in values):
        raise ValueError("Invalid embedding")
    norm = sqrt(sum(value * value for value in values))
    if not norm or not isfinite(norm):
        raise ValueError("Invalid embedding norm")
    return [value / norm for value in values]


class GeminiEmbeddings:
    def __init__(self, session: ClientSession, key: str) -> None:
        self.session = session
        self.key = key

    async def query(self, question: str) -> list[float]:
        result = await self._post("embedContent", self._input(question, "RETRIEVAL_QUERY"), timeout=12)
        try:
            return normalized_vector(result["embedding"]["values"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise EmbeddingUnavailable from None

    async def documents(self, inputs: list[tuple[str, str]]) -> list[list[float]]:
        requests = [
            {**self._input(content, "RETRIEVAL_DOCUMENT"), "title": title, "model": f"models/{EMBEDDING_MODEL}"}
            for title, content in inputs
        ]
        result = await self._post("batchEmbedContents", {"requests": requests}, timeout=45)
        try:
            vectors = [normalized_vector(item["values"]) for item in result["embeddings"]]
            if len(vectors) != len(inputs):
                raise ValueError("Unexpected embedding count")
            return vectors
        except (KeyError, TypeError, ValueError, OverflowError):
            raise EmbeddingUnavailable from None

    @staticmethod
    def _input(text: str, task_type: str) -> dict:
        return {
            "content": {"parts": [{"text": text}]},
            "taskType": task_type,
            "outputDimensionality": DIMENSIONS,
        }

    async def _post(self, operation: str, payload: dict, timeout: int) -> dict:
        try:
            async with self.session.post(
                f"{BASE_URL}:{operation}",
                headers={"x-goog-api-key": self.key},
                json=payload,
                timeout=ClientTimeout(total=timeout),
                allow_redirects=False,
                raise_for_status=True,
            ) as response:
                return await response.json()
        except ClientResponseError as error:
            LOGGER.warning("Gemini embeddings: HTTP %s", error.status)
            raise EmbeddingUnavailable(error.status) from None
        except (ClientError, TimeoutError, ValueError) as error:
            LOGGER.warning("Gemini embeddings: %s", type(error).__name__)
            raise EmbeddingUnavailable from None
