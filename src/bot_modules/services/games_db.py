import asyncio
import sqlite3
from pathlib import Path

from bot_modules.core.db_utils import open_db


class GamesDb:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def execute(self, query: str, params=()) -> sqlite3.Cursor:
        def _run():
            with open_db(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(query, params)
                conn.commit()
                return cur
        return await asyncio.to_thread(_run)

    async def fetchone(self, query: str, params=()) -> sqlite3.Row | None:
        def _run():
            with open_db(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                return conn.execute(query, params).fetchone()
        return await asyncio.to_thread(_run)

    async def fetchall(self, query: str, params=()) -> list[sqlite3.Row]:
        def _run():
            with open_db(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                return conn.execute(query, params).fetchall()
        return await asyncio.to_thread(_run)

    async def executemany(self, query: str, params_list) -> None:
        def _run():
            with open_db(self._db_path) as conn:
                conn.executemany(query, params_list)
                conn.commit()
        await asyncio.to_thread(_run)

    async def lastrowid(self, query: str, params=()) -> int:
        def _run() -> int:
            with open_db(self._db_path) as conn:
                cur = conn.execute(query, params)
                conn.commit()
                return cur.lastrowid or 0
        return await asyncio.to_thread(_run)
