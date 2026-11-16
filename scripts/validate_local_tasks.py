"""Opt-in synthetic tasks through the real model, authenticated API and native execution."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx

from server.config import Settings
from server.core.provider.openai_compat import OpenAICompatProvider


async def main(selected=None):
    baseline = Settings()
    capacity = await OpenAICompatProvider(baseline.llm_base_url, baseline.llm_api_key).context_window(baseline.llm_model)
    if capacity is None:
        raise RuntimeError("model context metadata unavailable")
    root = (Path(".runtime-task-validation") / (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])).resolve()
    root.mkdir(parents=True)
    settings = baseline.model_copy(update={
        "database_url": "sqlite+aiosqlite:///" + (root / "runtime.db").as_posix(),
        "control_token_path": str(root / "control.token"), "workspaces_dir": str(root / "workspaces"),
        "execution_snapshots_dir": str(root / "checkpoints"), "execution_policy_dir": str(root / "policies"),
        "execution_output_dir": str(root / "output"), "execution_receipts_dir": str(root / "receipts"),
        "runtime_archive_dir": str(root / "archives"), "skills_dir": str(root / "skills"),
        "memory_space_uid": "validation-" + uuid4().hex,
        "start_memory_sidecar": True, "memory_required": True,
        "atom_memory_base_url": "http://127.0.0.1:8020",
        "atom_memory_database_url": "sqlite:///" + (root / "memory.db").as_posix(),
        "atom_memory_sidecar_log": str(root / "memory.log"),
        "start_local_llm_sidecar": False, "local_llm_enabled": False,
        "computer_enabled": True, "execution_location": "local", "execution_backend": "native",
        "execution_allow_host": False, "execution_allow_public_network": False, "execution_read_allowlist": [],
        "execution_require_approval": False, "execution_snapshots_enabled": True, "api_auth_enabled": True,
        "mcp_enabled": False, "xwiki_enabled": False, "web_search_enabled": False, "tts_enabled": False,
        "reminders_enabled": False, "agent_context_window": capacity, "agent_max_steps": None,
        "agent_run_timeout_seconds": None, "agent_output_reserve": 4096,
    })
    # Only this process's isolated application uses these settings.
    from server import main as application
    application.get_settings = lambda: settings
    for name in ("httpx", "httpcore", "velora"):
        logging.getLogger(name).setLevel(logging.WARNING)
    app = application.create_app()
    report = {"model": settings.llm_model, "root": str(root), "production_config_changed": False,
              "real_model": True, "native_execution": True, "selected_case": selected, "cases": []}
    def save():
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    async with app.router.lifespan_context(app):
        report["memory_available"] = settings.memory_space_uid in app.state.velora.memory._ready
        report["memory_started_by_velora"] = app.state.velora.memory_sidecar.started_by_us
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
            base_url="http://validation", headers={"X-Velora-Token": app.state.velora.control_token}, timeout=180) as client:
            async def session():
                response = await client.post("/api/sessions", json={"tts_enabled": False})
                response.raise_for_status()
                identifier = response.json()["id"]
                workspace = root / "workspaces" / identifier
                workspace.mkdir(parents=True, exist_ok=True)
                return identifier, workspace
            async def run(identifier, text, acceptance=()):
                response = await client.post("/api/chat/stream", json={"session_id": identifier, "text": text, "acceptance": list(acceptance)})
                response.raise_for_status()
                frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                (root / (identifier + "-events.json")).write_text(json.dumps(frames, ensure_ascii=False, indent=2), encoding="utf-8")
                endings = [frame for frame in frames if isinstance(frame.get("result"), dict) and "status" in frame["result"]]
                if not endings:
                    raise AssertionError("task did not provide a terminal result")
                return endings[-1], frames
            async def case(name, action):
                start = perf_counter()
                try:
                    details = await asyncio.wait_for(action(), 150)
                    record = {"case": name, "passed": True, **details}
                except Exception as exc:
                    record = {"case": name, "passed": False, "error_type": type(exc).__name__, "detail": str(exc)[:400]}
                    active = list(app.state.velora.tasks.active.values())
                    for task_id in list(app.state.velora.tasks.active):
                        app.state.velora.tasks.cancel(task_id)
                    if active:
                        await asyncio.wait_for(asyncio.gather(*active, return_exceptions=True), 30)
                record["seconds"] = round(perf_counter() - start, 2)
                report["cases"].append(record)
                save()
                print(json.dumps(record, ensure_ascii=False), flush=True)

            async def csv_task():
                identifier, workspace = await session()
                (workspace / "orders.csv").write_text("order_id,customer,amount\n1,Alice,10.00\n2,Bob,7.25\n1,Alice,10.00\n3,Alice,5.50\n4,Chen,invalid\n5,Chen,0.00\n", encoding="utf-8")
                command = "import runpy; runpy.run_path('process_orders.py', run_name='__main__')"
                ending, frames = await run(identifier,
                    "处理工作目录 orders.csv。使用标准库编写可重复运行的 process_orders.py：按 order_id 去重，金额使用 Decimal，忽略非法金额。生成 report.json，字段 valid_rows、skipped_duplicates、skipped_invalid、totals（客户金额保留两位小数的字符串）；同时生成中文 report.md。两个报告都由脚本生成。最后必须用 run_python 执行以下完全一致的代码并根据结果总结：\n" + command,
                    [{"kind": "file", "path": "report.json", "json_valid": True}, {"kind": "file", "path": "report.md", "contains": "Alice"},
                     {"kind": "command", "operation": "run_python", "source": command}])
                assert ending["result"]["status"] == "completed", ending["result"]
                data = json.loads((workspace / "report.json").read_text(encoding="utf-8-sig"))
                assert data == {"valid_rows": 4, "skipped_duplicates": 1, "skipped_invalid": 1,
                                "totals": {"Alice": "15.50", "Bob": "7.25", "Chen": "0.00"}}, data
                assert ending["result"]["verification"]["status"] == "passed"
                return {"run_id": ending["result"]["run_id"], "steps": ending["result"]["step"], "actual_report_verified": True}

            async def repair_task():
                identifier, workspace = await session()
                (workspace / "billing.py").write_text("def total(prices, discount=0):\n    return sum(prices) * (1 + discount)\n", encoding="utf-8")
                tests = '''import unittest
from decimal import Decimal
from billing import total
class BillingTests(unittest.TestCase):
    def test_precision(self): self.assertEqual(total(['0.10', '0.20']), Decimal('0.30'))
    def test_discount(self): self.assertEqual(total(['10.00', '2.50'], '0.20'), Decimal('10.00'))
    def test_rounding(self): self.assertEqual(total(['1.005']), Decimal('1.01'))
    def test_empty(self): self.assertEqual(total([]), Decimal('0.00'))
    def test_negative(self):
        with self.assertRaises(ValueError): total(['-1.00'])
    def test_discount_range(self):
        with self.assertRaises(ValueError): total(['1.00'], '1.01')
    def test_no_mutation(self):
        values=['1.00','2.00']; total(values); self.assertEqual(values,['1.00','2.00'])
'''
                (workspace / "test_billing.py").write_text(tests, encoding="utf-8")
                test_digest = hashlib.sha256((workspace / "test_billing.py").read_bytes()).hexdigest()
                command = "import unittest; result=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('.', pattern='test_*.py')); assert result.wasSuccessful()"
                ending, _ = await run(identifier,
                    "修复工作目录 billing.py 的 total：接受字符串价格和折扣，用 Decimal 精确计算，折扣为 0..1，禁止负价格，金额使用 ROUND_HALF_UP 保留两位小数，空列表返回 Decimal('0.00')，不得修改输入。读取现有测试，不得修改测试文件，不安装依赖。最后必须用 run_python 执行以下完全一致的代码：\n" + command,
                    [{"kind": "command", "operation": "run_python", "source": command},
                     {"kind": "file", "path": "test_billing.py", "sha256": test_digest}])
                assert ending["result"]["status"] == "completed", ending["result"]
                assert hashlib.sha256((workspace / "test_billing.py").read_bytes()).hexdigest() == test_digest
                from server.core.execution.service import execute
                verified = json.loads(await execute(settings, identifier, "run_python", {"code": command},
                    execution_id=uuid4().hex, store=app.state.velora.router.execution_store, snapshots=app.state.velora.router.snapshots))
                assert verified.get("ok") and "Ran 7 tests" in verified.get("stderr", ""), verified
                return {"run_id": ending["result"]["run_id"], "steps": ending["result"]["step"], "tests_passed": 7, "test_file_unchanged": True}

            async def long_output_task():
                identifier, workspace = await session()
                sentinel = "TAIL_" + uuid4().hex
                (workspace / "tail.txt").write_text(sentinel, encoding="utf-8")
                command = "print('x' * 20000); print(open('tail.txt', encoding='utf-8').read())"
                ending, frames = await run(identifier,
                    "验证长输出读取。先用 run_python 执行下面代码（必须原样，不直接读取或修改 tail.txt，不生成其他脚本），然后用 read_execution_output 从偏移 20000 读取保留输出，最后告诉我尾部 TAIL_ 开头的标记。不要重跑原命令，不用 read_file：\n" + command,
                    [{"kind": "command", "operation": "run_python", "source": command}])
                assert ending["result"]["status"] == "completed", ending["result"]
                assert sentinel in ending.get("assistant_text", ""), "tail sentinel was not recovered"
                calls = [f for f in frames if "arguments" in f and "name" in f]
                assert sum(f["name"] == "run_python" for f in calls) == 1, "original command was repeated"
                assert any(f["name"] == "read_execution_output" for f in calls), "output paging tool not used"
                return {"run_id": ending["result"]["run_id"], "steps": ending["result"]["step"], "command_runs": 1, "tail_recovered": True}

            async def budget_task():
                identifier, workspace = await session()
                from server.core.nodes.llm import LLMNode
                runner = next(node for node in app.state.velora.router.executor.nodes if isinstance(node, LLMNode))
                original = runner._max_steps
                runner._max_steps = 1
                try:
                    ending, _ = await run(identifier, "必须先调用 write_file 创建 budget.txt，内容 ready，然后调用 read_file 核对，再总结完成情况。不要直接回答。")
                finally:
                    runner._max_steps = original
                assert ending["result"]["status"] == "failed" and ending["result"]["reason"] == "step_budget_exhausted", ending["result"]
                assert ending.get("assistant_text", "") == ""
                return {"run_id": ending["result"]["run_id"], "reason": ending["result"]["reason"], "false_success": False}

            async def cancel_task():
                identifier, workspace = await session()
                request = asyncio.create_task(run(identifier,
                    "只调用一次 run_python 执行以下代码，不拆分、不替换成线程，执行结束再回答：\nimport pathlib,time; pathlib.Path('started.txt').write_text('ready'); time.sleep(40); pathlib.Path('after.txt').write_text('should-not-exist')"))
                try:
                    for _ in range(900):
                        if (workspace / "started.txt").exists() or request.done():
                            break
                        await asyncio.sleep(.05)
                    assert (workspace / "started.txt").exists(), "workload did not start before cancellation deadline"
                    tasks = (await client.get("/api/chat/tasks", params={"session_id": identifier})).json()["tasks"]
                    response = await client.post("/api/chat/tasks/" + tasks[0]["task_id"] + "/cancel")
                    response.raise_for_status()
                    ending, _ = await asyncio.wait_for(request, 20)
                    assert ending["result"]["status"] == "cancelled", ending["result"]
                    assert not (workspace / "after.txt").exists()
                    return {"run_id": ending["result"]["run_id"], "status": "cancelled", "post_cancel_file_absent": True}
                finally:
                    if not request.done():
                        await client.post("/api/chat/cancel", json={"session_id": identifier})
                        request.cancel()
                        await asyncio.gather(request, return_exceptions=True)

            async def package_task():
                identifier, workspace = await session()
                (workspace / "pricing").mkdir()
                (workspace / "pricing" / "__init__.py").write_text("", encoding="utf-8")
                (workspace / "pricing" / "money.py").write_text(
                    "from decimal import Decimal\ndef total(values):\n    return sum(float(v) for v in values)\n", encoding="utf-8")
                command = "from pricing.money import total; from decimal import Decimal; result = total(['0.10', '0.20']); assert isinstance(result, Decimal); assert result == Decimal('0.30'); print('PACKAGE_OK')"
                ending, _ = await run(identifier,
                    "修复 pricing/money.py 的 total：输入为十进制金额字符串列表，精确累加并返回 Decimal，空列表返回 Decimal('0')。保留包结构。修复后必须通过 run_python 原样执行以下代码，不得修改 sys.path 或使用 shell 绕过：\n" + command,
                    [{"kind": "command", "operation": "run_python", "source": command}])
                assert ending["result"]["status"] == "completed", ending["result"]
                assert ending["result"]["verification"]["status"] == "passed"
                # Independent checks include inputs not supplied to the agent's final command.
                import subprocess, sys
                checked = subprocess.run([sys.executable, "-I", "-c",
                    "import sys; sys.path.insert(0, '.'); from pricing.money import total; from decimal import Decimal; assert total([]) == Decimal('0'); assert total(['100000000.01', '-100000000', '0.02']) == Decimal('0.03')"],
                    cwd=workspace, capture_output=True, text=True, timeout=10)
                assert checked.returncode == 0, checked.stderr
                return {"run_id": ending["result"]["run_id"], "independent_checks_passed": True}

            for name, action in (("package_decimal", package_task), ("csv_cleanup", csv_task), ("repair_python", repair_task), ("long_output", long_output_task),
                                 ("budget_failure", budget_task), ("cancel_workload", cancel_task)):
                if selected is None or selected == name:
                    await case(name, action)
    print(json.dumps({"report": str(root / "report.json"), "passed": sum(c["passed"] for c in report["cases"]), "total": len(report["cases"])}, ensure_ascii=False), flush=True)
    if not all(c["passed"] for c in report["cases"]):
        raise SystemExit(1)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("package_decimal", "csv_cleanup", "repair_python", "long_output", "budget_failure", "cancel_workload"))
    asyncio.run(main(parser.parse_args().case))
