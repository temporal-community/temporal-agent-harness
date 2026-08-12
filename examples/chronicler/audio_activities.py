"""Exact-script synthesis and WAV inspection for the Chronicler audio path."""

import base64
import binascii
import hashlib
import io
import os
import wave

from google import genai
from google.genai import types
from temporalio import activity
from temporalio.exceptions import ApplicationError

from examples.chronicler.audio_models import AudioApprovalPackage, SynthesizedWav

TTS_MODEL = os.environ.get("CHRONICLER_TTS_MODEL", "gemini-2.5-flash-preview-tts")
_VOICE = "Charon"
_PCM_SAMPLE_RATE_HZ = 24_000
_PCM_CHANNELS = 1
_PCM_SAMPLE_WIDTH_BYTES = 2


def _client() -> genai.Client:
    """Create the Gemini client explicitly from the worker's required API key."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ApplicationError(
            "GEMINI_API_KEY is not set on the worker",
            type="MissingApiKey",
            non_retryable=True,
        )
    return genai.Client(api_key=api_key)


def _pcm_to_wav_bytes(pcm: bytes) -> bytes:
    """Wrap Gemini's 24 kHz mono 16-bit PCM response in a WAV container."""
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(_PCM_CHANNELS)
        wav.setsampwidth(_PCM_SAMPLE_WIDTH_BYTES)
        wav.setframerate(_PCM_SAMPLE_RATE_HZ)
        wav.writeframes(pcm)
    return output.getvalue()


@activity.defn(name="chronicler_audio_synthesize")
async def synthesize_approved_audio(
    package: AudioApprovalPackage,
) -> SynthesizedWav:
    """Synthesize the exact approved script, then inspect the WAV independently."""
    client = _client()
    async with client.aio:
        response = await client.aio.models.generate_content(
            model=TTS_MODEL,
            contents=package.recap_script,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=_VOICE
                        )
                    )
                ),
            ),
        )
    try:
        pcm = response.candidates[0].content.parts[0].inline_data.data
    except (AttributeError, IndexError, TypeError) as error:
        raise ValueError("synthesis response did not contain PCM audio") from error
    if not isinstance(pcm, bytes) or not pcm:
        raise ValueError("synthesis response did not contain PCM audio")
    wav_bytes = _pcm_to_wav_bytes(pcm)
    audio_base64 = base64.b64encode(wav_bytes).decode("ascii")
    try:
        wav_bytes = base64.b64decode(audio_base64, validate=True)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            frames = wav.getnframes()
            sample_rate_hz = wav.getframerate()
            channels = wav.getnchannels()
            sample_width_bytes = wav.getsampwidth()
            pcm_data = wav.readframes(frames)
    except (binascii.Error, EOFError, wave.Error) as error:
        raise ValueError("synthesis output is not a valid WAV") from error
    if (
        frames <= 0
        or sample_rate_hz <= 0
        or channels <= 0
        or sample_width_bytes <= 0
        or len(pcm_data) != frames * channels * sample_width_bytes
    ):
        raise ValueError("synthesis output is not a usable WAV")

    return SynthesizedWav(
        script=package.recap_script,
        voice=_VOICE,
        audio_base64=audio_base64,
        wav_hash=hashlib.sha256(wav_bytes).hexdigest(),
        wav_size=len(wav_bytes),
        duration_s=frames / sample_rate_hz,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
        sample_width_bytes=sample_width_bytes,
    )
