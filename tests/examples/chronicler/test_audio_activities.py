import asyncio
import hashlib
import io
from types import SimpleNamespace
import wave

import pytest
from temporalio.exceptions import ApplicationError

from examples.chronicler import audio_activities
from examples.chronicler.audio_models import AudioApprovalPackage, SynthesizedWav


def _approved_package() -> AudioApprovalPackage:
    content = "The party entered the crypt."
    return AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7/transcript.json",
        source_content=content,
        source_hash=hashlib.sha256(content.encode()).hexdigest(),
        recap_script="The exact approved recap.",
        voice="Charon",
        wav_path="audio/session-7-recap.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )


def _response(pcm: bytes):
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[SimpleNamespace(inline_data=SimpleNamespace(data=pcm))]
                )
            )
        ]
    )


class _Models:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _AsyncClient:
    def __init__(self, models):
        self.models = models

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        del args


class _Client:
    def __init__(self, models):
        self.aio = _AsyncClient(models)


def _install_client(monkeypatch: pytest.MonkeyPatch, response):
    models = _Models(response)
    monkeypatch.setattr(
        audio_activities,
        "_client",
        lambda: _Client(models),
    )
    return models


class _LifecycleModels:
    def __init__(self, response, async_client):
        self.response = response
        self.async_client = async_client
        self.open_during_request = False

    async def generate_content(self, **kwargs):
        del kwargs
        self.open_during_request = not self.async_client.closed
        if self.async_client.closed:
            raise AssertionError("async client closed before provider response")
        return self.response


class _LifecycleAsyncClient(_AsyncClient):
    def __init__(self):
        self.closed = False
        self.entered = 0
        self.exited = 0
        self.models = None

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *args):
        del args
        self.exited += 1
        self.closed = True


class _LifecycleClient:
    def __init__(self, async_client):
        self.aio = async_client

    def __del__(self):
        self.aio.closed = True


def _install_lifecycle_client(monkeypatch: pytest.MonkeyPatch, response):
    async_client = _LifecycleAsyncClient()
    models = _LifecycleModels(response, async_client)
    async_client.models = models
    monkeypatch.setattr(
        audio_activities,
        "_client",
        lambda: _LifecycleClient(async_client),
    )
    return async_client, models


def _wav_bytes(*, frames: int = 2_000, rate: int = 8_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * frames)
    return output.getvalue()


def test_synthesis_uses_exact_script_model_and_charon(monkeypatch) -> None:
    package = _approved_package()
    pcm = b"\x00\x00" * 12_000
    models = _install_client(monkeypatch, _response(pcm))

    result = asyncio.run(audio_activities.synthesize_approved_audio(package))

    assert len(models.calls) == 1
    call = models.calls[0]
    assert call["model"] == audio_activities.TTS_MODEL
    assert call["contents"] == package.recap_script
    assert (
        call["config"].speech_config.voice_config.prebuilt_voice_config.voice_name
        == "Charon"
    )
    assert isinstance(result, SynthesizedWav)
    assert result.script == package.recap_script
    assert result.voice == "Charon"
    assert result.duration_s == 0.5
    assert result.sample_rate_hz == 24_000
    assert result.channels == 1
    assert result.sample_width_bytes == 2


def test_synthesis_keeps_async_client_open_until_provider_response(monkeypatch) -> None:
    async_client, models = _install_lifecycle_client(
        monkeypatch, _response(b"\x00\x00" * 12_000)
    )

    result = asyncio.run(audio_activities.synthesize_approved_audio(_approved_package()))

    assert result.script == _approved_package().recap_script
    assert models.open_during_request is True
    assert async_client.entered == 1
    assert async_client.exited == 1
    assert async_client.closed is True


def test_client_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(ApplicationError, match="GEMINI_API_KEY") as raised:
        audio_activities._client()

    assert raised.value.type == "MissingApiKey"
    assert raised.value.non_retryable is True


@pytest.mark.parametrize("response", [SimpleNamespace(candidates=[]), _response(b"")])
def test_synthesis_rejects_missing_provider_pcm(monkeypatch, response) -> None:
    _install_client(monkeypatch, response)

    with pytest.raises(ValueError, match="did not contain PCM audio"):
        asyncio.run(audio_activities.synthesize_approved_audio(_approved_package()))


def test_synthesis_rejects_invalid_wrapped_wav(monkeypatch) -> None:
    _install_client(monkeypatch, _response(b"\x00\x00"))
    monkeypatch.setattr(audio_activities, "_pcm_to_wav_bytes", lambda pcm: b"not WAV")

    with pytest.raises(ValueError, match="valid WAV"):
        asyncio.run(audio_activities.synthesize_approved_audio(_approved_package()))


def test_synthesis_rejects_zero_frame_wav(monkeypatch) -> None:
    _install_client(monkeypatch, _response(b"\x00\x00"))
    monkeypatch.setattr(
        audio_activities, "_pcm_to_wav_bytes", lambda pcm: _wav_bytes(frames=0)
    )

    with pytest.raises(ValueError, match="usable WAV"):
        asyncio.run(audio_activities.synthesize_approved_audio(_approved_package()))


def test_synthesis_hashes_and_measures_wrapped_wav_deterministically(monkeypatch) -> None:
    _install_client(monkeypatch, _response(b"\x00\x00"))
    valid = _wav_bytes(frames=4_000, rate=8_000)
    monkeypatch.setattr(audio_activities, "_pcm_to_wav_bytes", lambda pcm: valid)

    first = asyncio.run(audio_activities.synthesize_approved_audio(_approved_package()))
    second = asyncio.run(audio_activities.synthesize_approved_audio(_approved_package()))

    assert first.wav_hash == second.wav_hash == hashlib.sha256(valid).hexdigest()
    assert first.wav_size == len(valid)
    assert first.duration_s == second.duration_s == 0.5
