"""Local execution lifecycle, independent of chat transport."""
import asyncio
import uuid
import json

from server.core.execution.service import execute
from server.core.execution.sandbox import sandbox_status
from server.core.execution.artifacts import OutputStore


class ExecutionEnvironment:
    def __init__(self, settings, store, snapshots, busy=None):
        self.settings, self.store, self.snapshots = settings, store, snapshots
        self.tasks = {}
        self.busy = busy if busy is not None else set()
        self.bindings = {}

    def capabilities(self):
        return {**sandbox_status(self.settings), "operations": ["run_shell", "run_python", "read_file", "write_file", "list_dir"],
                "lifecycle": ["submit", "inspect", "cancel", "events", "artifacts"],
                "reconciliation": "durable_receipt_only", "ownership": "single_service_process",
                "approval_transport": "interactive_chat_only"}

    async def submit(self, session_id, operation, arguments, execution_id=None):
        identifier = execution_id or uuid.uuid4().hex
        if identifier in self.tasks:
            if self.bindings[identifier] != (session_id, operation, arguments):
                raise ValueError("execution identifier conflicts with an active request")
            return identifier
        if session_id in self.busy:
            raise ValueError("session already has active work")
        previous = await self.store.read(identifier)
        if session_id in self.busy:
            raise ValueError("session already has active work")
        self.busy.add(session_id)
        task = asyncio.create_task(execute(self.settings, session_id, operation, arguments, execution_id=identifier,
            store=self.store, snapshots=self.snapshots if self.settings.execution_snapshots_enabled else None, redact=(self.settings.llm_api_key,)))
        self.tasks[identifier] = task
        self.bindings[identifier] = (session_id, operation, arguments)
        def finished(value):
            self.tasks.pop(identifier, None)
            self.bindings.pop(identifier, None)
            self.busy.discard(session_id)
            if not value.cancelled():
                value.exception()
        task.add_done_callback(finished)
        if previous is not None:
            result = json.loads(await asyncio.shield(task))
            if result.get("reason") == "execution_conflict":
                raise ValueError("execution identifier conflicts with a persisted request")
        # Establish the durable claim before returning the identifier.
        while not task.done() and await self.store.read(identifier) is None:
            await asyncio.sleep(.01)
        if task.done() and await self.store.read(identifier) is None:
            payload = json.loads(task.result())
            raise ValueError(payload.get("error", "execution could not be recorded"))
        return identifier

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def output(self, execution_id, session_id, channel="stdout", offset=0, limit=12000):
        return OutputStore(self.settings.execution_output_dir, self.settings.execution_output_storage_bytes).read(execution_id, session_id, channel, offset, limit)
