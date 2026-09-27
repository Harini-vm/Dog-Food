"""All configuration comes from environment variables with offline-safe defaults."""

import os
from pathlib import Path


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/dogfood")
BASE_URL = os.environ.get("DOGFOOD_BASE_URL", "http://localhost:8080").rstrip("/")
SECRET_KEY = os.environ.get("DOGFOOD_SECRET_KEY", "dev-secret")
DATA_DIR = Path(os.environ.get("DOGFOOD_DATA_DIR", ".data"))
FIXTURES = Path(os.environ.get("DOGFOOD_FIXTURES", "fixtures.json"))
DEMO = _flag("DOGFOOD_DEMO", "true")
SEED = _flag("DOGFOOD_SEED", "true")
SECURE_COOKIES = _flag("DOGFOOD_SECURE_COOKIES", "false")
