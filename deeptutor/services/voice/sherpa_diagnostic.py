"""Measure a local sherpa-onnx model against one PCM WAV file."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import platform
import time

from deeptutor.services.voice.adapters.sherpa_onnx import (
    SherpaOnnxSTTAdapter,
    _read_pcm16_wav,
)
from deeptutor.services.voice.config import STTConfig


async def diagnose(model_dir: Path, audio_path: Path, *, runs: int = 1) -> dict[str, object]:
    if runs < 1:
        raise ValueError("runs must be positive")
    audio = audio_path.read_bytes()
    _, _, audio_seconds = _read_pcm16_wav(audio)
    adapter = SherpaOnnxSTTAdapter()
    config = STTConfig(
        model=str(model_dir),
        provider_name="sherpa_onnx",
        adapter="sherpa_onnx",
        language="ja",
    )
    measurements: list[dict[str, object]] = []
    for run in range(1, runs + 1):
        started = time.perf_counter()
        transcript = await adapter.transcribe(
            audio,
            config,
            filename=audio_path.name,
            content_type="audio/wav",
        )
        elapsed = time.perf_counter() - started
        measurements.append(
            {
                "run": run,
                "elapsed_seconds": round(elapsed, 6),
                "real_time_factor": round(elapsed / audio_seconds, 6) if audio_seconds else None,
                "transcript": transcript,
            }
        )
    first = measurements[0]
    return {
        "status": "completed",
        "model_dir": str(model_dir.resolve()),
        "audio_path": str(audio_path.resolve()),
        "audio_seconds": round(audio_seconds, 6),
        "elapsed_seconds": first["elapsed_seconds"],
        "real_time_factor": first["real_time_factor"],
        "transcript": first["transcript"],
        "measurements": measurements,
        "platform": platform.platform(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure local Japanese sherpa-onnx transcription and emit JSON."
    )
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    try:
        result = asyncio.run(diagnose(args.model_dir, args.audio, runs=args.runs))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
