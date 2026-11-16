"""Run Vertin's existing CLI and count tokens yielded by its speech decoder."""

import runpy
import sys
from pathlib import Path
from time import perf_counter

script = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(script.parent))
sys.argv[0] = str(script)
cli = runpy.run_path(str(script))
original = cli["CosyVoice3"]
tokens = 0
seconds = 0.0


def load_voice(*args, **kwargs):
    voice = original(*args, **kwargs)
    inference = voice.model.llm.inference

    def measured(*args, **kwargs):
        global tokens, seconds
        started = perf_counter()
        try:
            for token in inference(*args, **kwargs):
                tokens += 1
                yield token
        finally:
            seconds += perf_counter() - started

    voice.model.llm.inference = measured
    return voice


cli["main"].__globals__["CosyVoice3"] = load_voice
cli["main"]()
print(f"VELORA_TTS_METRICS {tokens} {seconds}", flush=True)
