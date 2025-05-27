from __future__ import annotations as _annotations

import asyncio
import json
import sqlite3
import os
from collections.abc import AsyncIterator
from concurrent.futures.thread import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, TypeVar

import fastapi
import logfire
from fastapi import Depends, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from typing_extensions import LiteralString, ParamSpec, TypedDict

from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
import pydantic_ai.models.openai as openai
from dotenv import load_dotenv
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

load_dotenv()

# 'if-token-present' means nothing will be sent (and the example will work) if you don't have logfire configured
logfire.configure(send_to_logfire='if-token-present')
logfire.instrument_pydantic_ai()

# Initialize the agent with environment variable
openai_api_key = os.getenv('OPENAI_API_KEY')
if not openai_api_key:
    raise ValueError("OPENAI_API_KEY environment variable is not set")

agent = Agent('openai:gpt-4o')
THIS_DIR = Path(__file__).parent

# For Vercel deployment, we'll use an in-memory SQLite database
DB_PATH = Path('/tmp/chat_app_messages.sqlite') if os.getenv('VERCEL') else THIS_DIR / '.chat_app_messages.sqlite'

@asynccontextmanager
async def lifespan(_app: fastapi.FastAPI):
    try:
        async with Database.connect() as db:
            yield {'db': db}
    except Exception as e:
        print(f"Database connection error: {e}")
        yield {'db': None}

app = fastapi.FastAPI(lifespan=lifespan)
logfire.instrument_fastapi(app)

@app.get('/')
async def index() -> FileResponse:
    return FileResponse((THIS_DIR / 'chat_app.html'), media_type='text/html')

@app.get('/chat_app.ts')
async def main_ts() -> FileResponse:
    return FileResponse((THIS_DIR / 'chat_app.ts'), media_type='text/plain')

async def get_db(request: Request) -> Database:
    db = request.state.db
    if db is None:
        raise fastapi.HTTPException(status_code=500, detail="Database connection failed")
    return db

@app.get('/chat/')
async def get_chat(database: Database = Depends(get_db)) -> Response:
    try:
        msgs = await database.get_messages()
        return Response(
            b'\n'.join(json.dumps(to_chat_message(m)).encode('utf-8') for m in msgs),
            media_type='text/plain',
        )
    except Exception as e:
        raise fastapi.HTTPException(status_code=500, detail=str(e))

class ChatMessage(TypedDict):
    """Format of messages sent to the browser."""

    role: Literal['user', 'model']
    timestamp: str
    content: str


def to_chat_message(m: ModelMessage) -> ChatMessage:
    first_part = m.parts[0]
    if isinstance(m, ModelRequest):
        if isinstance(first_part, UserPromptPart):
            assert isinstance(first_part.content, str)
            return {
                'role': 'user',
                'timestamp': first_part.timestamp.isoformat(),
                'content': first_part.content,
            }
    elif isinstance(m, ModelResponse):
        if isinstance(first_part, TextPart):
            return {
                'role': 'model',
                'timestamp': m.timestamp.isoformat(),
                'content': first_part.content,
            }
    raise UnexpectedModelBehavior(f'Unexpected message type for chat app: {m}')


@app.post('/chat/')
async def post_chat(
    prompt: Annotated[str, fastapi.Form()], database: Database = Depends(get_db)
) -> StreamingResponse:
    try:
        async def stream_messages():
            yield (
                json.dumps(
                    {
                        'role': 'user',
                        'timestamp': datetime.now(tz=timezone.utc).isoformat(),
                        'content': prompt,
                    }
                ).encode('utf-8')
                + b'\n'
            )
            messages = await database.get_messages()
            async with agent.run_stream(prompt, message_history=messages) as result:
                async for text in result.stream(debounce_by=0.01):
                    m = ModelResponse(parts=[TextPart(text)], timestamp=result.timestamp())
                    yield json.dumps(to_chat_message(m)).encode('utf-8') + b'\n'
            await database.add_messages(result.new_messages_json())

        return StreamingResponse(stream_messages(), media_type='text/plain')
    except Exception as e:
        raise fastapi.HTTPException(status_code=500, detail=str(e))


P = ParamSpec('P')
R = TypeVar('R')


@dataclass
class Database:
    """Rudimentary database to store chat messages in SQLite.

    The SQLite standard library package is synchronous, so we
    use a thread pool executor to run queries asynchronously.
    """

    con: sqlite3.Connection
    _loop: asyncio.AbstractEventLoop
    _executor: ThreadPoolExecutor

    @classmethod
    @asynccontextmanager
    async def connect(cls, file: Path = DB_PATH) -> AsyncIterator[Database]:
        with logfire.span('connect to DB'):
            loop = asyncio.get_event_loop()
            executor = ThreadPoolExecutor(max_workers=1)
            con = await loop.run_in_executor(executor, cls._connect, file)
            slf = cls(con, loop, executor)
        try:
            yield slf
        finally:
            await slf._asyncify(con.close)

    @staticmethod
    def _connect(file: Path) -> sqlite3.Connection:
        con = sqlite3.connect(str(file))
        con = logfire.instrument_sqlite3(con)
        cur = con.cursor()
        cur.execute(
            'CREATE TABLE IF NOT EXISTS messages (id INT PRIMARY KEY, message_list TEXT);'
        )
        con.commit()
        return con

    async def add_messages(self, messages: bytes):
        await self._asyncify(
            self._execute,
            'INSERT INTO messages (message_list) VALUES (?);',
            messages,
            commit=True,
        )
        await self._asyncify(self.con.commit)

    async def get_messages(self) -> list[ModelMessage]:
        c = await self._asyncify(
            self._execute, 'SELECT message_list FROM messages order by id'
        )
        rows = await self._asyncify(c.fetchall)
        messages: list[ModelMessage] = []
        for row in rows:
            messages.extend(ModelMessagesTypeAdapter.validate_json(row[0]))
        return messages

    def _execute(
        self, sql: LiteralString, *args: Any, commit: bool = False
    ) -> sqlite3.Cursor:
        cur = self.con.cursor()
        cur.execute(sql, args)
        if commit:
            self.con.commit()
        return cur

    async def _asyncify(
        self, func: Callable[P, R], *args: P.args, **kwargs: P.kwargs
    ) -> R:
        return await self._loop.run_in_executor(  # type: ignore
            self._executor,
            partial(func, **kwargs),
            *args,  # type: ignore
        )