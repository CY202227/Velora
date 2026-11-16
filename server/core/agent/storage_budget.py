"""Admission headroom for local runtime persistence, reserving room to report failure."""
from sqlalchemy import text


class StorageBudgetExceeded(ValueError):
    pass


class RuntimeStorageBudget:
    def __init__(self, engine, max_bytes, reserve_bytes=16 * 1024 * 1024):
        self.engine, self.max_bytes, self.reserve_bytes = engine, max_bytes, reserve_bytes

    async def check(self):
        if self.engine.dialect.name != "sqlite":
            return
        async with self.engine.connect() as connection:
            size = (await connection.execute(text("PRAGMA page_size"))).scalar_one()
            pages = (await connection.execute(text("PRAGMA page_count"))).scalar_one()
            free = (await connection.execute(text("PRAGMA freelist_count"))).scalar_one()
        if (pages - free) * size > self.max_bytes - self.reserve_bytes:
            raise StorageBudgetExceeded("运行存储预算不足，任务未启动或已停止；请归档历史或提高预算后继续。")
