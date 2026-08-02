"""Optional offline sherpa-onnx speech-to-text adapter."""

from __future__ import annotations

from array import array
import asyncio
import io
import os
from pathlib import Path
import sys
import threading
from typing import Any
import wave

from deeptutor.services.voice.base import BaseSTTAdapter, VoiceProviderError
from deeptutor.services.voice.config import STTConfig


def _first_model_file(model_dir: Path, *patterns: str) -> Path:
    for pattern in patterns:
        matches = sorted(model_dir.glob(pattern))
        if matches:
            return matches[0]
    raise VoiceProviderError(
        f"sherpa-onnx model is incomplete in {model_dir}: expected {patterns[0]}"
    )


def _read_pcm16_wav(audio: bytes) -> tuple[int, list[float], float]:
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.getnframes()
            payload = wav.readframes(frames)
    except (EOFError, wave.Error) as exc:
        raise VoiceProviderError("sherpa-onnx requires a valid PCM WAV upload.") from exc
    if channels != 1 or sample_width != 2 or sample_rate <= 0:
        raise VoiceProviderError("sherpa-onnx requires mono 16-bit PCM WAV audio.")
    samples = array("h")
    samples.frombytes(payload)
    if sys.byteorder != "little":
        samples.byteswap()
    normalized = [sample / 32768.0 for sample in samples]
    duration = len(normalized) / sample_rate
    return sample_rate, normalized, duration


class SherpaOnnxSTTAdapter(BaseSTTAdapter):
    """Run a transducer model locally on PCM WAV without network access."""

    def __init__(self) -> None:
        self._recognizers: dict[str, Any] = {}
        self._lock = threading.Lock()

    async def transcribe(
        self,
        audio: bytes,
        config: STTConfig,
        *,
        filename: str = "audio.wav",
        content_type: str = "audio/wav",
    ) -> str:
        if not audio:
            raise VoiceProviderError("No audio data to transcribe.")
        if not filename.lower().endswith(".wav") and "wav" not in content_type.lower():
            raise VoiceProviderError("sherpa-onnx requires PCM WAV audio.")
        return await asyncio.to_thread(self._transcribe_sync, audio, config)

    def _transcribe_sync(self, audio: bytes, config: STTConfig) -> str:
        sample_rate, samples, _ = _read_pcm16_wav(audio)
        recognizer = self._recognizer(config.model)
        stream = recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples)
        recognizer.decode_stream(stream)
        return str(stream.result.text or "").strip()

    def _recognizer(self, model_value: str):
        model_dir = Path(model_value).expanduser().resolve()
        if not model_dir.is_dir():
            raise VoiceProviderError(f"sherpa-onnx model directory does not exist: {model_dir}")
        cache_key = str(model_dir)
        with self._lock:
            cached = self._recognizers.get(cache_key)
            if cached is not None:
                return cached
            try:
                import sherpa_onnx
            except ImportError as exc:
                raise VoiceProviderError(
                    "sherpa-onnx is not installed; install DeepTutor with the voice-local extra."
                ) from exc
            encoder = _first_model_file(model_dir, "encoder*.int8.onnx", "encoder*.onnx")
            decoder = _first_model_file(model_dir, "decoder*.int8.onnx", "decoder*.onnx")
            joiner = _first_model_file(model_dir, "joiner*.int8.onnx", "joiner*.onnx")
            tokens = _first_model_file(model_dir, "tokens.txt")
            thread_value = os.getenv("DEEPTUTOR_SHERPA_NUM_THREADS", "").strip()
            try:
                threads = int(thread_value) if thread_value else min(4, os.cpu_count() or 1)
            except ValueError as exc:
                raise VoiceProviderError(
                    "DEEPTUTOR_SHERPA_NUM_THREADS must be a positive integer."
                ) from exc
            if threads <= 0:
                raise VoiceProviderError("DEEPTUTOR_SHERPA_NUM_THREADS must be positive.")
            try:
                recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=str(encoder),
                    decoder=str(decoder),
                    joiner=str(joiner),
                    tokens=str(tokens),
                    num_threads=threads,
                    sample_rate=16000,
                    feature_dim=80,
                    decoding_method="greedy_search",
                    provider="cpu",
                )
            except Exception as exc:
                raise VoiceProviderError(f"failed to load sherpa-onnx model: {exc}") from exc
            self._recognizers[cache_key] = recognizer
            return recognizer


__all__ = ["SherpaOnnxSTTAdapter", "_read_pcm16_wav"]
