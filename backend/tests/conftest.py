import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base, get_db
from app.main import app
from app.models import entities  # noqa: F401


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # Not entered as a context manager: the app's lifespan calls
    # Base.metadata.create_all() against the real (Postgres) engine, which
    # isn't reachable in tests. Table creation for the test DB is handled
    # above against the SQLite engine instead.
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()
    engine.dispose()
