"""Opt-in live model canary using synthetic input and a separate local database."""

import asyncio
import json
from pathlib import Path
from time import perf_counter

from server.config import Settings
from server.core.agent.compaction import prepare_context
from server.core.agent.state import RunPhase, RunState
from server.core.agent.store import RunStore
from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult, TurnRequest
from server.core.conversation.store import ConversationStore
from server.core.nodes.llm import LLMNode
from server.core.provider.openai_compat import OpenAICompatProvider
from server.core.tools.registry import ToolRegistry
from server.core.tools.types import ToolSpec


async def main(selected=None):
    settings = Settings()
    root = Path('.runtime-canary')
    root.mkdir(exist_ok=True)
    provider = OpenAICompatProvider(settings.llm_base_url, settings.llm_api_key, timeout=60, max_output_tokens=512)
    capacity = await provider.context_window(settings.llm_model)
    if capacity is None:
        raise RuntimeError('Model context capacity unavailable')
    db = ConversationStore('sqlite+aiosqlite:///' + (root.resolve() / 'canary.db').as_posix())
    await db.init()
    store = RunStore(db._session_factory)
    report = {'model': settings.llm_model, 'context_window': capacity, 'production_config_changed': False, 'cases': []}
    async def case(name, action):
        start = perf_counter()
        try:
            details = await asyncio.wait_for(action(), 120)
            report['cases'].append({'case': name, 'passed': True, 'seconds': round(perf_counter()-start, 2), **(details or {})})
        except Exception as exc:
            report['cases'].append({'case': name, 'passed': False, 'error_type': type(exc).__name__,
                                    'seconds': round(perf_counter()-start, 2), **getattr(exc, 'canary_details', {})})
        (root / (f'report-{selected}.json' if selected else 'report.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report['cases'][-1], ensure_ascii=False), flush=True)
    def context(text):
        return TurnContext(request=TurnRequest('canary', text), session_id='canary', model=settings.llm_model,
                           llm_base_url=settings.llm_base_url, llm_api_key=settings.llm_api_key,
                           messages=[{'role': 'user', 'content': text}])
    def node(tools=None):
        return LLMNode(provider, tools=tools, context_window=capacity, output_reserve=512, run_timeout=90)
    async def plain():
        ctx = context('Reply with exactly CANARY_OK and nothing else.')
        ctx.checkpoint = lambda boundary: store.checkpoint(ctx, boundary)
        assert await node().process(ctx) is NodeResult.CONTINUE
        assert 'CANARY_OK' in ctx.assistant_text
        assert (await store.read(ctx.agent_run.run_id))['run']['outcome']['status'] == 'completed'
        return {'steps': ctx.agent_run.step, 'input_tokens': ctx.agent_run.input_tokens, 'output_tokens': ctx.agent_run.output_tokens}
    async def tool():
        registry = ToolRegistry()
        async def add(args, ctx):
            return json.dumps({'ok': True, 'value': int(args['a']) + int(args['b'])})
        registry.register(ToolSpec(name='canary_add', description='Add two integers for this test.', parameters={'type':'object','properties':{'a':{'type':'integer'},'b':{'type':'integer'}},'required':['a','b']}, handler=add))
        ctx = context('You must call canary_add with a=17 and b=25. Then report the returned number.')
        ctx.extras['tools'] = registry.openai_tools()
        assert await node(registry).process(ctx) is NodeResult.CONTINUE
        assert ctx.agent_run.tool_executions and '42' in ctx.assistant_text
        return {'steps': ctx.agent_run.step, 'tools': len(ctx.agent_run.tool_executions)}
    async def cancel():
        ctx = context('Write a detailed essay about sorting algorithms.')
        entered = asyncio.Event()
        async def emit(event):
            if event.type == 'token':
                entered.set()
        ctx.emit = emit
        submitted = asyncio.Event()
        runner = node()
        sample = runner._stream_round
        async def observed_request(*args, **kwargs):
            submitted.set()
            return await sample(*args, **kwargs)
        runner._stream_round = observed_request
        task = asyncio.create_task(runner.process(ctx))
        try:
            await asyncio.wait_for(submitted.wait(), 15)
            await asyncio.sleep(0.5)
            if task.done():
                raise RuntimeError('Request finished before cancellation could be tested')
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert ctx.agent_run.outcome.status.value == 'cancelled'
            return {'visible_token_received': entered.is_set(), 'cancelled_while_request_in_flight': True}
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async def resume():
        ctx = context('Reply with exactly RECOVERY_OK.')
        ctx.agent_run = RunState(messages=ctx.messages, phase=RunPhase.REQUESTING, step=1)
        await store.checkpoint(ctx, 'request_ready')
        claimed = await store.claim(ctx.agent_run.run_id)
        restored = context(ctx.request.user_text)
        restored.extras['resume_checkpoint'] = claimed
        restored.checkpoint = lambda boundary: store.checkpoint(restored, boundary)
        assert await node().process(restored) is NodeResult.CONTINUE
        assert 'RECOVERY_OK' in restored.assistant_text
        return {'steps': restored.agent_run.step}
    async def closed_tool_resume():
        ctx = context('Use the saved tool result. Reply with exactly CLOSED_RECOVERY_OK_42.')
        result = json.dumps({'ok': True, 'value': 42})
        ctx.messages.append({'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'saved', 'type': 'function', 'function': {'name': 'canary_saved', 'arguments': '{}'}}]})
        ctx.agent_run = RunState(messages=ctx.messages, phase=RunPhase.EXECUTING_TOOLS, step=1)
        ctx.agent_run.tool_executions = [{'id': 'saved', 'name': 'canary_saved', 'round': 0,
                                          'status': 'returned', 'result': result}]
        await store.checkpoint(ctx, 'tool_returned')
        claimed = await store.claim(ctx.agent_run.run_id)
        assert claimed['resume_next_step']
        restored = context(ctx.request.user_text)
        restored.extras['resume_checkpoint'] = claimed
        restored.checkpoint = lambda boundary: store.checkpoint(restored, boundary)
        assert await node().process(restored) is NodeResult.CONTINUE
        assert 'CLOSED_RECOVERY_OK_42' in restored.assistant_text
        assert restored.agent_run.step == 2 and len(restored.agent_run.tool_executions) == 1
        return {'steps': restored.agent_run.step, 'tools_reexecuted': 0}

    async def compact():
        diagnostics = {}
        class SummaryProbe:
            def __getattr__(self, name):
                return getattr(provider, name)
            async def chat(self, *args, **kwargs):
                reply = await provider.chat(*args, **kwargs)
                diagnostics.update(summary_finish_reason=reply.finish_reason,
                                   summary_has_text=bool((reply.content or '').strip()),
                                   summary_output_tokens=reply.usage.output_tokens if reply.usage else None)
                return reply
        state = RunState(context_window=capacity, messages=[
            {'role':'system','content':'Follow current user request.'},
            {'role':'user','content':'Remember the confirmed result is 42; preserve it in your summary.'},
            {'role':'assistant','content':'The confirmed result is 42. ' * 800,'tool_calls':[{'id':'old','type':'function','function':{'name':'lookup','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'old','content':'Confirmed result: 42'},
            {'role':'assistant','tool_calls':[{'id':'new','type':'function','function':{'name':'lookup','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'new','content':'Latest result: ready'},
        ])
        try:
            await prepare_context(state, SummaryProbe(), settings.llm_model, None, capacity-6000, summary_tokens=512)
        except Exception as exc:
            from server.core.agent.compaction import ContextBudgetError
            if isinstance(exc, ContextBudgetError):
                exc.canary_details = {'reason': str(exc), **diagnostics}
            raise
        assert state.compactions == 1 and '42' in state.messages[2]['content']
        return {'compactions':state.compactions, 'context_tokens':state.estimated_input_tokens}
    try:
        for name, action in [('plain',plain),('tool_roundtrip',tool),('cancel',cancel),('safe_resume',resume),('closed_tool_resume',closed_tool_resume),('summary',compact)]:
            if selected is None or selected == name:
                await case(name, action)
    finally:
        await db.close()
    if any(not c['passed'] for c in report['cases']):
        raise SystemExit(1)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=('plain', 'tool_roundtrip', 'cancel', 'safe_resume', 'closed_tool_resume', 'summary'))
    asyncio.run(main(parser.parse_args().case))
