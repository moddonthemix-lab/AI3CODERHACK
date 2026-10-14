import io
import json
import wave

from builder.voice import speakable, tts_engine, wav_info

from .test_cli_server import _req, _serve  # noqa: F401  (server helpers)


def _wav(seconds=0.5, rate=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def test_speakable_strips_citations_links_and_markdown():
    text = ("## Answer\n**Prices rose ~12%** in 2025 [S3] [S7] — see <https://bls.gov/x>. F12 is verified.\n"
            "| a | b |\n|---|---|\n- Margins are thin & falling. One. Two. Three. Four.")
    out = speakable(text)
    assert "[S" not in out and "http" not in out and "|" not in out and "**" not in out and "F12" not in out
    assert out.startswith("Answer Prices rose about 12 percent in 2025")
    assert out.endswith("The full details are on screen.")
    assert speakable("Suppliers matter [F1, S1, S11]. Quantities may differ (S2).") == "Suppliers matter. Quantities may differ."
    assert speakable("It reached $412 million, or $240 a pack — see <https://x.org/y>.") == \
        "It reached 412 million dollars, or 240 dollars a pack."


def test_tts_engine_prefers_piper_then_builtin(monkeypatch, tmp_path):
    import builder.voice as v
    voice = tmp_path / "v.onnx"
    voice.write_bytes(b"x")
    monkeypatch.setattr(v.shutil, "which", lambda n: f"/usr/bin/{n}" if n in ("piper", "espeak-ng") else None)
    assert tts_engine(str(voice)) == "piper"
    assert tts_engine("") == "espeak"
    monkeypatch.setattr(v.sys, "platform", "darwin")
    monkeypatch.setattr(v.shutil, "which", lambda n: "/usr/bin/say" if n == "say" else None)
    assert tts_engine("") == "say"


class FakeVoice:
    def synthesize(self, text):
        return _wav(0.1)

    def transcribe(self, audio):
        assert wav_info(bytes(audio))["rate"] == 16000
        return "how big is the battery market"


class FakeAssistant:
    def ask(self, text, from_agent=""):
        return f"About $412 million [S1], answering: {text}"


def test_voice_endpoints_for_an_esp32_terminal():
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from builder.db import Store
    from builder.server import make_handler
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store(), FakeAssistant(), None, voice=FakeVoice()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        req = urllib.request.Request(base + "/api/listen", data=_wav(), method="POST",
                                     headers={"Content-Type": "audio/wav", "X-From": "esp32"})
        body = json.loads(urllib.request.urlopen(req).read())
        assert body["heard"] == "how big is the battery market" and "[S1]" in body["reply"]
        assert "[S1]" not in body["speak"] and body["audio"]["seconds"] == 0.5
        req = urllib.request.Request(base + "/api/speak", data=json.dumps({"text": "Hello"}).encode(), method="POST")
        with urllib.request.urlopen(req) as r:
            assert r.headers["Content-Type"] == "audio/wav" and wav_info(r.read())["channels"] == 1
        bad = urllib.request.Request(base + "/api/listen", data=b"not audio", method="POST")
        try:
            urllib.request.urlopen(bad)
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        httpd.shutdown()


def test_to_rate_for_esp32_speakers():
    from builder.voice import to_rate
    out = to_rate(_wav(1.0, rate=22050), 16000)
    assert wav_info(out) == {"channels": 1, "rate": 16000, "seconds": 1.0}
