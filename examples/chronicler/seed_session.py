"""Seed a short, fake D&D session recording so the example is runnable without a real recording.

Uses Gemini multi-speaker TTS to voice a ~1-minute scripted scene (a DM and a player), writes it
to ``sessions/session-01.wav``, and registers it in ``sessions/sessions.json``. Then the
Chronicler agent has something real to transcribe → summarize → recap.

Run from the repo root:
    uv run --group examples python -m examples.chronicler.seed_session
(or `just seed` from examples/chronicler)

Needs GEMINI_API_KEY. This is a plain script (not a Temporal activity) — it just calls the SDK.
"""

from __future__ import annotations

import json
import os
import wave
from pathlib import Path

from google import genai
from google.genai import types

from .chronicler_activities import TTS_MODEL

SESSIONS_DIR = Path(__file__).parent / "sessions"
AUDIO_PATH = SESSIONS_DIR / "session-01.wav"
REGISTRY_PATH = SESSIONS_DIR / "sessions.json"

# A short scripted scene to voice as the "recording". Speaker tags match the multi-speaker config.
SCRIPT = """\
DM: The tavern door groans open. Behind the bar, a one-eyed dwarf named Borin Ironhand eyes your
muddy boots. "You're the ones asking about the Whispering Crypt," he grunts.
Rogue: I slide a gold piece across the bar and lean in. "Depends who's asking. What do you know
about the crypt, Borin?"
DM: Borin pockets the coin. "The Duskblade cult took it over a fortnight past. Folk who go up the
hill don't come back. But if you're set on it — take the old goat path, not the road. The road's
watched." He lowers his voice. "And whatever you do, don't ring the black bell."
Rogue: "Naturally, the first thing I want to do now is ring the black bell. But fine. The goat
path it is."
"""


def _wave(path: Path, pcm: bytes, rate: int = 24000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not set (put it in examples/chronicler/.env.local)")
    client = genai.Client(api_key=api_key)
    print(f"Synthesizing a sample session with {TTS_MODEL} ...", flush=True)
    resp = client.models.generate_content(
        model=TTS_MODEL,
        contents=SCRIPT,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                    speaker_voice_configs=[
                        types.SpeakerVoiceConfig(
                            speaker="DM",
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon")
                            ),
                        ),
                        types.SpeakerVoiceConfig(
                            speaker="Rogue",
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Puck")
                            ),
                        ),
                    ]
                )
            ),
        ),
    )
    pcm = resp.candidates[0].content.parts[0].inline_data.data
    _wave(AUDIO_PATH, pcm)
    print(f"Wrote {AUDIO_PATH} ({len(pcm) / (24000 * 2):.1f}s)", flush=True)

    registry = {
        "sessions": [
            {
                "session_id": "session-01",
                "campaign_id": "duskblade",
                "title": "The Whispering Crypt (Session 1)",
                "recorded_at": "2026-07-01",
                "number": 1,
                "audio_file": "session-01.wav",
            }
        ]
    }
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2) + "\n")
    print(f"Registered session-01 in {REGISTRY_PATH}", flush=True)
    print('Done. Try asking Chronicler: "transcribe session 1, then give me a recap."', flush=True)


if __name__ == "__main__":
    main()
