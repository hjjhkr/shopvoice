"""Text-to-speech (fragment based): the <=15 s spoken summary in, one audio file out.

TTS_PROVIDER=pyttsx3 (default: offline system voices - SAPI5 on Windows, NSSpeech on macOS, eSpeak on Linux)
TTS_PROVIDER=openai  (TTS_MODEL e.g. gpt-4o-mini-tts or tts-1, TTS_VOICE e.g. alloy)
"""
from __future__ import annotations

import subprocess
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import Any

from ..config import get_settings

# pyttsx3 runs in a short-lived child process: its event loop is not safe to reuse
# across calls or to start from a non-main thread (Streamlit runs scripts in threads).
_PYTTSX3_WORKER = r"""
import sys, pyttsx3
text = open(sys.argv[1], encoding="utf-8").read()
engine = pyttsx3.init()
engine.setProperty("rate", int(sys.argv[3]))
voice = sys.argv[4] if len(sys.argv) > 4 else ""
if voice:
    for v in engine.getProperty("voices"):
        if voice.lower() in (v.name or "").lower() or voice == v.id:
            engine.setProperty("voice", v.id); break
engine.save_to_file(text, sys.argv[2])
engine.runAndWait()
"""


def _wav_seconds(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() / float(w.getframerate()), 2)
    except (wave.Error, EOFError, OSError):
        return None


def _pyttsx3(text: str, out: Path, voice: str) -> None:
    txt = out.with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    try:
        subprocess.run([sys.executable, "-c", _PYTTSX3_WORKER, str(txt), str(out), "175", voice],
                       check=True, timeout=60, capture_output=True)
    finally:
        txt.unlink(missing_ok=True)


def _openai(text: str, out: Path, model: str, voice: str) -> None:
    from openai import OpenAI

    with OpenAI().audio.speech.with_streaming_response.create(
        model=model, voice=voice or "alloy", input=text, response_format="mp3"
    ) as response:
        response.stream_to_file(out)


def synthesize(text: str, out_path: str | Path | None = None) -> dict[str, Any]:
    """-> {path, provider, seconds, latency_ms}"""
    settings = get_settings()
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    provider = settings.tts_provider
    suffix = ".mp3" if provider == "openai" else ".wav"
    out = Path(out_path) if out_path else settings.audio_dir / f"tts_{uuid.uuid4().hex[:10]}{suffix}"
    t0 = time.perf_counter()
    if provider == "openai":
        _openai(text, out, settings.tts_model, settings.tts_voice)
        seconds = round(len(text.split()) / 2.6, 1)          # estimate (~155 wpm)
    else:
        _pyttsx3(text, out, settings.tts_voice)
        seconds = _wav_seconds(out)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"TTS provider '{provider}' produced no audio")
    return {"path": str(out), "provider": provider, "seconds": seconds,
            "latency_ms": round((time.perf_counter() - t0) * 1000)}
