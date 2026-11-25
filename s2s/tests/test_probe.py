import io
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from probe_litellm import pcm_duration_s, wav_sample_rate  # noqa: E402


def make_wav(rate: int, seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def test_wav_sample_rate():
    assert wav_sample_rate(make_wav(24000, 0.1)) == 24000


def test_wav_sample_rate_rejects_non_wav():
    with pytest.raises(ValueError):
        wav_sample_rate(b"ceci n'est pas un fichier WAV du tout")


def test_pcm_duration_s():
    assert pcm_duration_s(48000, 24000) == 1.0
