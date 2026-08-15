"""Optional Microsoft Edge online text-to-speech adapter."""

from __future__ import annotations

from deeptutor.services.voice.base import BaseTTSAdapter, VoiceProviderError
from deeptutor.services.voice.config import TTSConfig


class EdgeTTSAdapter(BaseTTSAdapter):
    """Synthesize MP3 through the optional ``edge-tts`` package.

    The dependency is imported at call time so installations that use another
    provider do not need it. Edge TTS is an online service and is intentionally
    registered as one exchangeable provider, never as a mandatory fallback.
    """

    async def synthesize(self, text: str, config: TTSConfig) -> tuple[bytes, str]:
        try:
            import edge_tts
        except ImportError as exc:
            raise VoiceProviderError(
                "edge-tts is not installed; install DeepTutor with the voice-edge extra."
            ) from exc

        voice = config.voice or "ja-JP-NanamiNeural"
        if config.speed is not None:
            percent = round((config.speed - 1.0) * 100)
        try:
            communicate = (
                edge_tts.Communicate(text, voice, rate=f"{percent:+d}%")
                if config.speed is not None
                else edge_tts.Communicate(text, voice)
            )
            chunks = [
                chunk["data"]
                async for chunk in communicate.stream()
                if chunk.get("type") == "audio" and chunk.get("data")
            ]
        except Exception as exc:
            raise VoiceProviderError(f"edge-tts synthesis failed: {exc}") from exc
        audio = b"".join(chunks)
        if not audio:
            raise VoiceProviderError("edge-tts returned empty audio.")
        return audio, "audio/mpeg"


__all__ = ["EdgeTTSAdapter"]
