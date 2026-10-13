"""Use the saved connection for a fixed public source-retrieval benchmark."""
import asyncio
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main():
    from scripts.memory_eval_runtime import saved_connection
    from atom_memory.config import settings
    connection = await saved_connection()
    settings.llm_base_url, settings.llm_api_key, settings.llm_model = (connection[key] for key in ("llm_base_url", "llm_api_key", "llm_model"))
    script, dataset, output = map(Path, sys.argv[1:4])
    spec = importlib.util.spec_from_file_location("source_benchmark", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = await asyncio.to_thread(module.main, [str(dataset), str(output), "--sample-index", "1", "--method", "llm", *sys.argv[4:]])
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
