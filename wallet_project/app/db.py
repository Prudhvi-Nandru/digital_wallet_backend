import os
from contextlib import contextmanager
from psycopg_pool import ConnectionPool

_pool = None

def pool():
    global _pool
    if _pool is None:
        _pool = ConnectionPool(os.getenv('DATABASE_URL','postgresql://wallet:wallet@localhost:5432/wallet'), min_size=1, max_size=20, open=True)
    return _pool

@contextmanager
def connection():
    with pool().connection() as conn:
        yield conn
