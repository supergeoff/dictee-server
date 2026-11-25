import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import compat_test as ct  # noqa: E402


def test_session_update_matches_bridge():
    ev = ct.session_update()
    assert ev["type"] == "session.update"
    session = ev["session"]
    assert session["type"] == "realtime"
    assert session["output_modalities"] == ["audio"]
    assert session["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert session["audio"]["output"]["voice"] == "fr-FR-Marc:MAI-Voice-2.1-Flash"
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert session["audio"]["input"]["transcription"] == {"model": "transcribe"}
    vad = session["audio"]["input"]["turn_detection"]
    assert vad["type"] == "server_vad"
    assert vad["create_response"] is False
    assert vad["interrupt_response"] is False
    assert session["tool_choice"] == "auto"
    names = [t["name"] for t in session["tools"]]
    assert names == ["generate_chat_completion", "play_animation"]
    chat = session["tools"][0]
    assert chat["type"] == "function"
    assert chat["parameters"]["required"] == ["request"]
    assert chat["parameters"]["additionalProperties"] is False
    anim = session["tools"][1]
    assert anim["parameters"]["properties"]["name"]["enum"] == [g["name"] for g in ct.GESTURES]
    assert anim["parameters"]["required"] == ["name"]


def test_session_update_takes_instructions_and_voice():
    ev = ct.session_update(instructions="Bonjour", voice="autre")
    assert ev["session"]["instructions"] == "Bonjour"
    assert ev["session"]["audio"]["output"]["voice"] == "autre"


def test_out_of_band_response():
    ev = ct.out_of_band_status("working")
    assert ev["type"] == "response.create"
    response = ev["response"]
    assert response["conversation"] == "none"
    assert response["input"] == []
    assert response["tools"] == []
    assert response["tool_choice"] == "none"
    assert response["metadata"] == {"status": "working"}
    assert "I am working on your request." in response["instructions"]


def test_respond_to_input_carries_item_id_metadata():
    ev = ct.respond_to_input("item_1")
    assert ev == {
        "type": "response.create",
        "response": {"metadata": {"input_item_id": "item_1"}},
    }


def test_respond_to_result_and_function_output():
    out = ct.function_call_output("call_1", "completed", "Il est 21 heures.")
    assert out["type"] == "conversation.item.create"
    assert out["item"]["type"] == "function_call_output"
    assert out["item"]["call_id"] == "call_1"
    assert json.loads(out["item"]["output"]) == {
        "status": "completed",
        "answer": "Il est 21 heures.",
    }
    ev = ct.respond_to_result("call_1")
    assert ev["type"] == "response.create"
    assert ev["response"]["metadata"] == {"call_id": "call_1"}
    assert ev["response"]["tool_choice"] == "auto"
    assert [t["name"] for t in ev["response"]["tools"]] == ["play_animation"]


def test_input_audio_buffer_clear_matches_bridge():
    """Le client d'Open WebUI l'envoie à chaque coupure du micro (src/lib/utils/realtime.ts) : type seul, rien d'autre."""
    assert ct.input_audio_buffer_clear() == {"type": "input_audio_buffer.clear"}


def test_point_10_is_the_muted_microphone_point():
    number, label, fn, timeout, needs = ct.POINTS[-1]
    assert (number, fn) == (10, ct.point10)
    assert "micro coupé" in label
    assert needs == ()


def test_context_events_first_snapshot_has_no_delete():
    events = ct.context_events(0, [{"role": "user", "content": "Salut"}])
    assert [e["type"] for e in events] == ["conversation.item.create"]
    item = events[0]["item"]
    assert item["id"] == "chat_context_1"
    assert item["type"] == "message"
    assert item["role"] == "system"
    assert item["content"][0]["type"] == "input_text"
    assert "Salut" in item["content"][0]["text"]


def test_context_events_next_snapshot_deletes_then_creates():
    events = ct.context_events(1, [{"role": "assistant", "content": "Bonjour"}])
    assert events[0] == {"type": "conversation.item.delete", "item_id": "chat_context_1"}
    assert events[1]["type"] == "conversation.item.create"
    assert events[1]["item"]["id"] == "chat_context_2"


def test_audio_append_is_valid_base64_pcm():
    pcm = b"\x01\x00" * 1200
    ev = ct.audio_append(pcm)
    assert set(ev) == {"type", "audio"}
    assert ev["type"] == "input_audio_buffer.append"
    assert base64.b64decode(ev["audio"], validate=True) == pcm


def test_audio_append_rejects_odd_or_oversized_chunks():
    with pytest.raises(ValueError):
        ct.audio_append(b"\x00")
    with pytest.raises(ValueError):
        ct.audio_append(b"\x00\x00" * 24001)
    with pytest.raises(ValueError):
        ct.audio_append(b"")


def test_chunk_pcm_is_between_20_and_100_ms():
    pcm = b"\x00\x00" * 24000
    chunks = list(ct.chunk_pcm(pcm, 50))
    assert b"".join(chunks) == pcm
    assert all(len(c) == 2400 for c in chunks)
    with pytest.raises(ValueError):
        list(ct.chunk_pcm(pcm, 10))
    with pytest.raises(ValueError):
        list(ct.chunk_pcm(pcm, 200))


def test_silence_duration():
    assert len(ct.silence(1.5)) == 72000


def test_short_truncates_audio_payloads():
    text = ct.short({"type": "response.output_audio.delta", "delta": "A" * 5000})
    assert len(text) < 300
    assert "response.output_audio.delta" in text


def test_check_input_pcm_accepts_24k_mono_16bit():
    ct.check_input_pcm(b"\x00\x00" * 2400)
    with pytest.raises(ValueError):
        ct.check_input_pcm(b"\x00")


def test_looks_french():
    assert ct.looks_french("Bonjour, peux-tu me dire quelle heure il est à Tokyo ?")
    assert not ct.looks_french("Hello, can you tell me the time in Tokyo?")


def test_parse_args_skip_gate_and_points():
    assert ct.parse_args([]) == (set(range(1, 11)), False)
    assert ct.parse_args(["--skip-gate"]) == (set(range(1, 11)), True)
    assert ct.parse_args(["--points", "2,7", "--skip-gate"]) == ({2, 7}, True)
    with pytest.raises(ValueError):
        ct.parse_args(["--points"])


def test_skip_gate_skips_point_1_and_needs_no_gate_key(monkeypatch, capsys):
    import asyncio

    async def fake_synthesize(client, base, key, text):
        return b"\x00\x00" * 2400

    monkeypatch.setattr(ct, "synthesize", fake_synthesize)
    monkeypatch.setenv("CONVERSATION_URL", "ws://127.0.0.1:1/v1/realtime")
    monkeypatch.delenv("CONVERSATION_GATE_KEY", raising=False)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.invalid/v1")
    monkeypatch.setenv("LITELLM_KEY", "test")
    assert asyncio.run(ct.run({1}, skip_gate=True)) == 0
    out = capsys.readouterr().out
    assert "SKIP 1" in out
    assert "non applicable en local" in out
    assert "PASS 1" not in out and "FAIL 1" not in out
    # Sans le drapeau, la clé du gardien reste obligatoire.
    assert asyncio.run(ct.run({1})) == 1
    assert "CONVERSATION_GATE_KEY" in capsys.readouterr().out
