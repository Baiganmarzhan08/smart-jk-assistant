from os import getenv
from pathlib import Path
from re import fullmatch

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"


def load_project_env() -> None:
    load_dotenv(PROJECT_DIR / ".env", encoding="utf-8-sig")


def load_token() -> str:
    load_project_env()
    token = getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Укажите TELEGRAM_BOT_TOKEN в файле .env.")
    return token


def load_gemini_settings() -> tuple[str, str]:
    api_key = getenv("GEMINI_API_KEY", "").strip()
    model = getenv("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    if api_key and not fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}", model):
        raise ValueError("Проверьте GEMINI_MODEL в файле .env. Укажите только имя модели.")
    return api_key, model
