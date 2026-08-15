"""Tests for the voice (TTS/STT) service layer.

Covers Markdown cleaning, the OpenAI-compatible adapters' wire shape, the
OpenRouter base64-JSON STT branch, Azure auth headers, and catalog-driven
config resolution.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from deeptutor.services.config.provider_runtime import (
    resolve_stt_runtime_config,
    resolve_tts_runtime_config,
)
from deeptutor.services.voice import synthesize_speech, transcribe_audio
from deeptutor.services.voice.adapters.edge_tts import EdgeTTSAdapter
from deeptutor.services.voice.adapters.openai_compat import (
    OpenAICompatSTTAdapter,
    OpenAICompatTTSAdapter,
    OpenRouterTTSAdapter,
)
from deeptutor.services.voice.adapters.sherpa_onnx import SherpaOnnxSTTAdapter
from deeptutor.services.voice.base import (
    build_auth_headers,
    join_audio_path,
    strip_markdown_for_speech,
)
from deeptutor.services.voice.config import STTConfig, TTSConfig


def _capture_post(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> dict[str, Any]:
    """Patch ``httpx.AsyncClient.post`` to record args and return ``response``."""
    captured: dict[str, Any] = {}

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["data"] = kwargs.get("data")
        captured["files"] = kwargs.get("files")
        captured["headers"] = kwargs.get("headers")
        response.request = httpx.Request("POST", url)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return captured


# ── text cleaning ─────────────────────────────────────────────────────────


def test_strip_markdown_drops_code_and_unwraps_links() -> None:
    md = "# Title\n\nHello **world**, read [the docs](http://x).\n\n```py\nprint(1)\n```\n- one\n- two"
    out = strip_markdown_for_speech(md)
    assert "Title" in out and "Hello world" in out and "the docs" in out
    assert "print(1)" not in out  # fenced code dropped
    assert "**" not in out and "[" not in out and "#" not in out


def test_strip_markdown_truncates_on_boundary() -> None:
    out = strip_markdown_for_speech("Sentence one. Sentence two. Sentence three.", max_chars=20)
    assert len(out) <= 20
    assert out.endswith(".")


def test_join_audio_path_appends_and_preserves_full_url() -> None:
    assert join_audio_path("https://api.openai.com/v1", "audio/speech").endswith("/v1/audio/speech")
    full = "https://r.azure.com/openai/deployments/tts/audio/speech?api-version=2025"
    assert join_audio_path(full, "audio/speech") == full


def test_auth_headers_styles() -> None:
    assert build_auth_headers("bearer", "k") == {"Authorization": "Bearer k"}
    assert build_auth_headers("api_key_header", "k") == {"api-key": "k"}
    assert build_auth_headers("token", "k") == {"Authorization": "Token k"}
    assert build_auth_headers("bearer", "") == {}


# ── TTS adapter ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tts_adapter_posts_openai_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = httpx.Response(200, content=b"ID3audio-bytes", headers={"content-type": "audio/mpeg"})
    captured = _capture_post(monkeypatch, resp)
    config = TTSConfig(
        model="gpt-4o-mini-tts",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        voice="alloy",
        response_format="mp3",
    )
    audio, content_type = await OpenAICompatTTSAdapter().synthesize("hi there", config)
    assert audio == b"ID3audio-bytes"
    assert content_type == "audio/mpeg"
    assert captured["url"] == "https://api.openai.com/v1/audio/speech"
    assert captured["json"] == {
        "model": "gpt-4o-mini-tts",
        "input": "hi there",
        "response_format": "mp3",
        "voice": "alloy",
    }
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_tts_adapter_azure_uses_api_key_header(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = httpx.Response(200, content=b"x", headers={"content-type": "audio/mpeg"})
    captured = _capture_post(monkeypatch, resp)
    config = TTSConfig(
        model="tts-1",
        base_url="https://r.azure.com/openai/deployments/tts/audio/speech?api-version=2025-04-01",
        api_key="azkey",
        auth_style="api_key_header",
        voice="alloy",
    )
    await OpenAICompatTTSAdapter().synthesize("hello", config)
    assert captured["headers"]["api-key"] == "azkey"
    assert "Authorization" not in captured["headers"]
    # Full /audio/ URL is preserved verbatim.
    assert captured["url"].endswith("api-version=2025-04-01")


@pytest.mark.asyncio
async def test_tts_adapter_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from deeptutor.services.voice.base import VoiceProviderError

    _capture_post(monkeypatch, httpx.Response(401, text="bad key"))
    config = TTSConfig(model="m", base_url="https://x/v1", api_key="k", voice="alloy")
    with pytest.raises(VoiceProviderError, match="401"):
        await OpenAICompatTTSAdapter().synthesize("hi", config)


@pytest.mark.asyncio
async def test_openrouter_tts_falls_back_to_chat_audio_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_calls: list[dict[str, Any]] = []

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        post_calls.append(
            {
                "url": url,
                "json": kwargs.get("json"),
                "headers": kwargs.get("headers"),
            }
        )
        if len(post_calls) == 1:
            response = httpx.Response(
                500,
                json={"error": {"message": "Internal Server Error"}},
            )
        else:
            chunk = {
                "choices": [
                    {
                        "delta": {
                            "audio": {
                                "data": base64.b64encode(b"pcm-audio").decode("ascii"),
                                "transcript": "hi",
                            }
                        }
                    }
                ]
            }
            response = httpx.Response(
                200,
                text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n",
                headers={"content-type": "text/event-stream"},
            )
        response.request = httpx.Request("POST", url)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    config = TTSConfig(
        model="openai/gpt-4o-mini-tts",
        provider_name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="or-key",
        voice="alloy",
        response_format="pcm",
    )

    audio, content_type = await OpenRouterTTSAdapter().synthesize("hello", config)

    assert audio == b"pcm-audio"
    assert content_type == "audio/pcm"
    assert post_calls[0]["url"] == "https://openrouter.ai/api/v1/audio/speech"
    assert post_calls[1]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert post_calls[1]["json"]["modalities"] == ["text", "audio"]
    assert post_calls[1]["json"]["audio"] == {"voice": "alloy", "format": "pcm16"}
    assert post_calls[1]["headers"]["Authorization"] == "Bearer or-key"


@pytest.mark.asyncio
async def test_openrouter_gemini_tts_openai_voice_gets_clear_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.services.voice.base import VoiceProviderError

    _capture_post(
        monkeypatch,
        httpx.Response(500, json={"error": {"message": "Internal Server Error"}}),
    )
    config = TTSConfig(
        model="google/gemini-3.1-flash-tts-preview",
        provider_name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="or-key",
        voice="alloy",
        response_format="pcm",
    )
    with pytest.raises(VoiceProviderError, match="Kore"):
        await OpenRouterTTSAdapter().synthesize("hello", config)


# ── STT adapter ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stt_adapter_multipart(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = httpx.Response(200, json={"text": "hello world"})
    captured = _capture_post(monkeypatch, resp)
    config = STTConfig(model="whisper-1", base_url="https://api.openai.com/v1", api_key="sk")
    text = await OpenAICompatSTTAdapter().transcribe(
        b"RIFFxxxx", config, filename="a.wav", content_type="audio/wav"
    )
    assert text == "hello world"
    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert captured["files"]["file"][0] == "a.wav"
    assert captured["data"]["model"] == "whisper-1"


@pytest.mark.asyncio
async def test_stt_adapter_openrouter_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = httpx.Response(200, json={"text": "from base64"})
    captured = _capture_post(monkeypatch, resp)
    config = STTConfig(
        model="openai/whisper-large-v3",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk",
        request_style="base64_json",
    )
    text = await OpenAICompatSTTAdapter().transcribe(
        b"audiobytes", config, filename="clip.webm", content_type="audio/webm"
    )
    assert text == "from base64"
    assert captured["files"] is None  # not multipart
    assert captured["json"]["model"] == "openai/whisper-large-v3"
    assert captured["json"]["input_audio"]["format"] == "webm"
    assert captured["json"]["input_audio"]["data"]  # base64 string present


# ── catalog resolution ────────────────────────────────────────────────────


def _voice_catalog() -> dict[str, Any]:
    return {
        "version": 1,
        "services": {
            "tts": {
                "active_profile_id": "p1",
                "active_model_id": "m1",
                "profiles": [
                    {
                        "id": "p1",
                        "binding": "siliconflow",
                        "base_url": "",
                        "api_key": "sf-key",
                        "models": [
                            {
                                "id": "m1",
                                "model": "FunAudioLLM/CosyVoice2-0.5B",
                                "voice": "FunAudioLLM/CosyVoice2-0.5B:anna",
                                "response_format": "wav",
                            }
                        ],
                    }
                ],
            },
            "stt": {
                "active_profile_id": "p2",
                "active_model_id": "m2",
                "profiles": [
                    {
                        "id": "p2",
                        "binding": "openrouter",
                        "base_url": "",
                        "api_key": "or-key",
                        "models": [{"id": "m2", "model": "openai/whisper-large-v3"}],
                    }
                ],
            },
        },
    }


def test_resolve_tts_config_uses_provider_default_base() -> None:
    cfg = resolve_tts_runtime_config(catalog=_voice_catalog())
    assert cfg.model == "FunAudioLLM/CosyVoice2-0.5B"
    assert cfg.provider_name == "siliconflow"
    assert cfg.base_url == "https://api.siliconflow.cn/v1"  # filled from spec default
    assert cfg.voice == "FunAudioLLM/CosyVoice2-0.5B:anna"
    assert cfg.response_format == "wav"
    assert cfg.api_key == "sf-key"


def test_resolve_stt_config_picks_openrouter_base64_style() -> None:
    cfg = resolve_stt_runtime_config(catalog=_voice_catalog())
    assert cfg.provider_name == "openrouter"
    assert cfg.request_style == "base64_json"
    assert cfg.base_url == "https://openrouter.ai/api/v1"


def test_resolve_tts_config_picks_openrouter_adapter() -> None:
    catalog = _voice_catalog()
    catalog["services"]["tts"]["profiles"][0]["binding"] = "openrouter"
    catalog["services"]["tts"]["profiles"][0]["models"][0]["model"] = (
        "google/gemini-3.1-flash-tts-preview"
    )
    cfg = resolve_tts_runtime_config(catalog=catalog)
    assert cfg.provider_name == "openrouter"
    assert cfg.adapter == "openrouter_tts"


def test_resolve_optional_voice_adapters_from_catalog() -> None:
    catalog = _voice_catalog()
    tts_profile = catalog["services"]["tts"]["profiles"][0]
    tts_profile["binding"] = "edge_tts"
    tts_profile["models"][0].update({"model": "edge-tts", "voice": "ja-JP-NanamiNeural"})
    stt_profile = catalog["services"]["stt"]["profiles"][0]
    stt_profile["binding"] = "sherpa_onnx"
    stt_profile["models"][0]["model"] = "/models/reazonspeech"

    tts = resolve_tts_runtime_config(catalog=catalog)
    stt = resolve_stt_runtime_config(catalog=catalog)

    assert tts.adapter == "edge_tts"
    assert tts.voice == "ja-JP-NanamiNeural"
    assert stt.adapter == "sherpa_onnx"
    assert stt.model == "/models/reazonspeech"


def test_resolve_tts_config_raises_without_model() -> None:
    catalog = {"version": 1, "services": {"tts": {"profiles": []}}}
    with pytest.raises(ValueError, match="No active TTS model"):
        resolve_tts_runtime_config(catalog=catalog)


# ── facade ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_speech_facade_strips_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = httpx.Response(200, content=b"audio", headers={"content-type": "audio/wav"})
    captured = _capture_post(monkeypatch, resp)
    audio, ctype = await synthesize_speech("# Hi\n\n**bold**", catalog=_voice_catalog())
    assert audio == b"audio"
    assert captured["json"]["input"] == "Hi\n\nbold"  # markdown stripped


@pytest.mark.asyncio
async def test_transcribe_audio_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = httpx.Response(200, json={"text": "transcribed"})
    _capture_post(monkeypatch, resp)
    text = await transcribe_audio(b"bytes", catalog=_voice_catalog(), filename="x.webm")
    assert text == "transcribed"


@pytest.mark.asyncio
async def test_edge_tts_adapter_is_lazy_and_collects_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from types import SimpleNamespace

    class Communicate:
        def __init__(self, text: str, voice: str, rate: str | None = None) -> None:
            assert text == "問題文"
            assert voice == "ja-JP-NanamiNeural"

        async def stream(self):
            yield {"type": "audio", "data": b"part-1"}
            yield {"type": "WordBoundary", "data": b"ignored"}
            yield {"type": "audio", "data": b"part-2"}

    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=Communicate))
    audio, content_type = await EdgeTTSAdapter().synthesize(
        "問題文",
        TTSConfig(
            model="edge-tts",
            provider_name="edge_tts",
            adapter="edge_tts",
            voice="ja-JP-NanamiNeural",
        ),
    )
    assert audio == b"part-1part-2"
    assert content_type == "audio/mpeg"


@pytest.mark.asyncio
async def test_sherpa_adapter_transcribes_pcm_wav_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import io
    import sys
    from types import SimpleNamespace
    import wave

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    for name in (
        "encoder-epoch-99-avg-1.int8.onnx",
        "decoder-epoch-99-avg-1.onnx",
        "joiner-epoch-99-avg-1.int8.onnx",
        "tokens.txt",
    ):
        (model_dir / name).write_bytes(b"fixture")

    captured: dict[str, Any] = {}

    class Stream:
        result = SimpleNamespace(text="二番")

        def accept_waveform(self, rate: int, samples: list[float]) -> None:
            captured["rate"] = rate
            captured["samples"] = samples

    class Recognizer:
        @classmethod
        def from_transducer(cls, **kwargs: Any):
            captured["config"] = kwargs
            return cls()

        def create_stream(self) -> Stream:
            return Stream()

        def decode_stream(self, stream: Stream) -> None:
            captured["decoded"] = True

    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(OfflineRecognizer=Recognizer))
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00\x01\x00" * 80)

    text = await SherpaOnnxSTTAdapter().transcribe(
        wav_buffer.getvalue(),
        STTConfig(
            model=str(model_dir),
            provider_name="sherpa_onnx",
            adapter="sherpa_onnx",
            language="ja",
        ),
        filename="candidate.wav",
        content_type="audio/wav",
    )
    assert text == "二番"
    assert captured["rate"] == 16000
    assert captured["decoded"] is True
    assert captured["config"]["num_threads"] >= 1
