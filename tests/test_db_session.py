from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from app.db import session as db_session
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


class FakeConnection:
    async def __aenter__(self) -> FakeConnection:
        return self

    async def __aexit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        return None

    async def execute(self, _statement: Any) -> None:
        return None


class FakeEngine:
    def connect(self) -> FakeConnection:
        return FakeConnection()

    async def dispose(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeFactory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self) -> FakeFactory:
        return self

    async def __aenter__(self) -> FakeSession:
        return await self.session.__aenter__()

    async def __aexit__(self, type_: Any, value: Any, traceback: Any) -> None:
        await self.session.__aexit__(type_, value, traceback)


@pytest.mark.asyncio
async def test_database_health_and_session_lifecycle() -> None:
    engine = FakeEngine()
    assert await db_session.check_database(cast(AsyncEngine, engine), 2.0)
    assert db_session.session_factory(cast(AsyncEngine, engine))

    read_session = FakeSession()
    factory = cast(async_sessionmaker[AsyncSession], FakeFactory(read_session))
    read_generator = db_session.get_read_db(factory)
    assert cast(Any, await read_generator.__anext__()) is read_session
    with pytest.raises(StopAsyncIteration):
        await read_generator.__anext__()
    assert read_session.rollbacks == 1

    write_session = FakeSession()
    write_generator = db_session.get_write_db(cast(async_sessionmaker[AsyncSession], FakeFactory(write_session)))
    assert cast(Any, await write_generator.__anext__()) is write_session
    with pytest.raises(StopAsyncIteration):
        await write_generator.__anext__()
    assert write_session.commits == 1


@pytest.mark.asyncio
async def test_write_session_rolls_back_on_consumer_error() -> None:
    session = FakeSession()
    generator = cast(
        Any,
        db_session.get_write_db(cast(async_sessionmaker[AsyncSession], FakeFactory(session))),
    )
    await generator.__anext__()
    with pytest.raises(RuntimeError, match="consumer failure"):
        await generator.athrow(RuntimeError("consumer failure"))
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_database_health_times_out_without_hanging() -> None:
    class SlowConnection(FakeConnection):
        async def execute(self, _statement: Any) -> None:
            await asyncio.sleep(0.05)

    class SlowEngine(FakeEngine):
        def connect(self) -> SlowConnection:
            return SlowConnection()

    assert not await db_session.check_database(cast(AsyncEngine, SlowEngine()), 0.001)
