import io
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from probe_litellm import pcm_duration_s, wav_data_info, wav_sample_rate  # noqa: E402


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


def test_wav_data_info():
    assert wav_data_info(make_wav(24000, 0.1)) == (24000, 1, 16, 4800)


def test_wav_data_info_stereo_8_bits():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(1)
        w.setframerate(8000)
        w.writeframes(b"\x80\x80" * 10)
    assert wav_data_info(buf.getvalue()) == (8000, 2, 8, 20)


def test_wav_data_info_caps_declared_size_to_present_bytes():
    wav = make_wav(24000, 0.1)
    assert wav_data_info(wav[:-1000]) == (24000, 1, 16, 3800)


def test_wav_sample_rate_rejects_non_wav():
    with pytest.raises(ValueError):
        wav_sample_rate(b"ceci n'est pas un fichier WAV du tout")


def test_wav_rejects_truncated_fmt_chunk():
    with pytest.raises(ValueError, match="tronqu"):
        wav_data_info(make_wav(24000, 0.1)[:30])


def test_wav_rejects_zero_rate():
    wav = bytearray(make_wav(24000, 0.1))
    wav[24:28] = (0).to_bytes(4, "little")
    with pytest.raises(ValueError, match="invalide"):
        wav_data_info(bytes(wav))


def test_wav_rejects_missing_fmt_chunk():
    data = b"RIFF" + (4 + 8 + 4).to_bytes(4, "little") + b"WAVE"
    data += b"data" + (4).to_bytes(4, "little") + b"\x00" * 4
    with pytest.raises(ValueError):
        wav_data_info(data)


def test_wav_rejects_missing_data_chunk():
    wav = make_wav(24000, 0.1)
    with pytest.raises(ValueError, match="data"):
        wav_data_info(wav[:36])


def test_pcm_duration_s():
    assert pcm_duration_s(48000, 24000) == 1.0


@pytest.mark.parametrize("rate", [0, -24000])
def test_pcm_duration_s_rejects_invalid_rate(rate):
    with pytest.raises(ValueError):
        pcm_duration_s(48000, rate)
