import os
import tempfile

# Configure an isolated, offline app before anything imports app.config.
_tmp = tempfile.mkdtemp(prefix="microlearning-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["ENABLED_SOURCES"] = ""
os.environ["CRAWL_INTERVAL_MINUTES"] = "0"
os.environ["CRAWL_ON_STARTUP"] = "false"
os.environ["ESTIMATE_READING_TIME"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as test_client:
        yield test_client
