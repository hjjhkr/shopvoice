"""Speech-to-text (fragment based): one recorded WAV/MP3 clip in, transcript + timestamps out.

ASR_PROVIDER=faster_whisper (default, local, no API key; model via ASR_MODEL, e.g. base.en, small, medium)
ASR_PROVIDER=openai         (OpenAI transcription API; ASR_MODEL e.g. whisper-1)
"""
from __future__ import annotations

import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..config import get_settings


@lru_cache(maxsize=2)
def _local_model(name: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel
    return WhisperModel(name, device=device, compute_type=compute_type)


def load_audio(path: Path, sample_rate: int = 16000):
    """Decode any WAV/MP3/M4A clip to 16 kHz mono float32 with PyAV (no system ffmpeg needed).
    Done here rather than inside faster-whisper so the code does not depend on which PyAV
    version faster-whisper's own decoder expects."""
    import av
    import numpy as np

    chunks = []
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        try:
            for out in resampler.resample(None):          # flush
                chunks.append(out.to_ndarray().reshape(-1))
        except Exception:  # noqa: BLE001 - some PyAV versions have nothing to flush
            pass
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def _transcribe_local(path: Path, model_name: str) -> dict[str, Any]:
    device = os.environ.get("ASR_DEVICE", "cpu")
    compute = os.environ.get("ASR_COMPUTE_TYPE", "int8" if device == "cpu" else "float16")
    model = _local_model(model_name, device, compute)
    # vad_filter trims silence; beam search + temperature fallback help with accents and noise
    segments, info = model.transcribe(load_audio(path), beam_size=5, vad_filter=True)
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments]
    return {"text": " ".join(s["text"] for s in segs).strip(), "segments": segs,
            "language": info.language, "duration": round(info.duration, 2)}


def _transcribe_openai(path: Path, model_name: str) -> dict[str, Any]:
    from openai import OpenAI

    client = OpenAI()
    with open(path, "rb") as f:
        if model_name == "whisper-1":
            res = client.audio.transcriptions.create(
                model=model_name, file=f, response_format="verbose_json", timestamp_granularities=["segment"])
            segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
                    for s in (res.segments or [])]
            return {"text": res.text.strip(), "segments": segs,
                    "language": getattr(res, "language", None), "duration": getattr(res, "duration", None)}
        res = client.audio.transcriptions.create(model=model_name, file=f)   # gpt-4o-transcribe: no timestamps
        return {"text": res.text.strip(), "segments": [], "language": None, "duration": None}


def transcribe(audio_path: str | Path) -> dict[str, Any]:
    """-> {text, segments:[{start,end,text}], language, duration, provider, model, latency_ms}"""
    settings = get_settings()
    path = Path(audio_path)
    if not path.exists():
        raise FileNotFoundError(path)
    t0 = time.perf_counter()
    if settings.asr_provider == "openai":
        out = _transcribe_openai(path, settings.asr_model or "whisper-1")
    else:
        out = _transcribe_local(path, settings.asr_model or "base.en")
    out.update(provider=settings.asr_provider, model=settings.asr_model,
               latency_ms=round((time.perf_counter() - t0) * 1000))
    return out
