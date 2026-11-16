from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult
from server.core.inference_log import record

logger = logging.getLogger(__name__)


class PostProcessNode:
    name = "PostProcess"

    def __init__(self, script: str = "", python: str = "") -> None:
        self.script = Path(script).resolve() if script else None
        self.python = python or sys.executable

    async def process(self, ctx: TurnContext) -> NodeResult:
        if not ctx.tts_enabled or self.script is None or not ctx.assistant_text:
            return NodeResult.SKIP
        voice = ((ctx.persona.import_meta or {}) if ctx.persona else {}).get("velora_voice", "vertin")
        if voice not in ("vertin", "lilya", "sonetto", "regulus"):
            return NodeResult.SKIP
        model = f"CosyVoice3 · {voice.title()}"
        started = perf_counter()
        root = Path(__file__).resolve().parents[3]
        output = root / "velora_data" / "speech" / f"{uuid4().hex}.wav"
        source = output.with_suffix(".txt")
        proc = None
        success = False
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(ctx.assistant_text, encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                self.python, str(Path(__file__).resolve().parents[1] / "vertin_infer.py"), str(self.script),
                "--speaker", voice, "--file", str(source), "--output", str(output),
                cwd=self.script.parent.parent,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode != 0 or not output.is_file():
                raise RuntimeError(f"Vertin exited with status {proc.returncode}")
            metric = next(
                (line for line in reversed(stdout.decode(errors="replace").splitlines())
                 if line.startswith("VELORA_TTS_METRICS ")),
                None,
            )
            if metric:
                _, count, duration = metric.split()
                record("tts", model, float(duration), int(count) or None, audio=output.name)
            else:
                record("tts", model, perf_counter() - started, audio=output.name)
            success = True
            await ctx.publish("tts_audio", {"url": f"/api/debug/speech/{output.name}"})
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            logger.warning("Vertin TTS failed (%s)", type(exc).__name__)
            record("tts", model, perf_counter() - started, status="error")
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
            source.unlink(missing_ok=True)
            if not success:
                output.unlink(missing_ok=True)
        return NodeResult.CONTINUE
