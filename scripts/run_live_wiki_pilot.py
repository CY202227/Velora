"""Run a bounded SciFact answer pilot using Velora's saved model connection."""
import asyncio
import os
import runpy
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

async def main():
    from server.config import Settings
    from server.core.conversation.store import ConversationStore
    store = ConversationStore(Settings().database_url)
    try:
        connection = {key: await store.get_setting(key) for key in ("llm_base_url", "llm_api_key", "llm_model")}
    finally:
        await store.close()
    if not all(connection.values()):
        raise RuntimeError("Configure a model in Velora")
    os.environ["OPENAI_BASE_URL"] = connection["llm_base_url"]
    os.environ["OPENAI_API_KEY"] = connection["llm_api_key"]
    os.environ["OPENAI_MODEL"] = connection["llm_model"]
    os.environ["XWIKI_LLM_MAX_RETRIES"] = "0"
    os.environ["XWIKI_LLM_TIMEOUT"] = "45"
    runner, archive, out = sys.argv[1:4]
    sys.argv = [runner, archive, "--out", out, "--live"]
    # Import after setting model environment; no credentials enter reports.
    await asyncio.to_thread(runpy.run_path, runner, run_name="__main__")

if __name__ == "__main__":
    asyncio.run(main())
