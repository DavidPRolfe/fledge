import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from fledge.db import migrate

ADMIN_URL = os.environ.get("FLEDGE_TEST_DATABASE_URL", "postgresql:///postgres")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def database_url() -> Iterator[str]:
    """A migrated throwaway database on the server at FLEDGE_TEST_DATABASE_URL."""
    name = f"fledge_test_{uuid.uuid4().hex}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=name)
    migrate(url)
    yield url
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
        )
