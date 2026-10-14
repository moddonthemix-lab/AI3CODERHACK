"""Scholar's voice — listening and speaking, all local (ported from Amo).

- Listening: faster-whisper speech-to-text (``base.en`` on a Mac, ``tiny.en`` on a Pi).
- Speaking, best available first:
    1. Piper — natural neural voices, fast even on a Pi (``SCHOLAR_PIPER_VOICE`` = path to a .onnx voice;
       ``en_GB-alan-medium`` suits a scholar)
    2. macOS ``say`` — built in, nothing to install (``SCHOLAR_SAY_VOICE``, e.g. Daniel)
    3. espeak-ng — built into Raspberry Pi OS (robotic, but it works)
    4. pyttsx3, or printing the text if nothing else is there
- Microphone: sounddevice, with simple energy-based end-of-speech detection.

Researchers write citations and tables; nobody wants those read aloud. ``speakable()`` turns a reply
into something worth hearing: no [S12] markers, links or markdown, and long reports become a short
spoken summary.

An ESP32-S3 with a microphone and a speaker can be Scholar's voice terminal: it records a WAV and posts it
to ``/api/listen``, then plays the WAV that ``/api/speak`` returns (see server.py).

Install the extras with ``pip install -e '.[voice]'``.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

SAMPLE_RATE = 16_000
CHUNK_SECONDS = 0.1


class VoiceUnavailable(RuntimeError):
    pass


def _require(module: str):
    try:
        return __import__(module)
    except ImportError as e:
        raise VoiceUnavailable(f"Voice needs '{module}'. Install voice extras: pip install -e '.[voice]'") from e


def speakable(text: str, max_sentences: int = 4) -> str:
    """Turn a written answer into something good to hear."""
    t = re.sub(r"[\[(](?:\s*[SFT]\d+\s*[,;]?)+[\])]", "", text or "")                 # [S3], [F1, S1, S11], (S2)
    t = re.sub(r"\b[SF]\d+\b", "", t)                                                 # stray S12 / F3
    t = re.sub(r"[(\[]?\s*(?:[—–-]\s*)?(?:see\s+|at\s+|via\s+)?<?https?://[^\s>)\]]+>?[)\]]?", "", t, flags=re.I)  # links
    t = re.sub(r"```.*?```", " ", t, flags=re.S)                                 # code
    t = re.sub(r"^\s*\|.*\|\s*$", "", t, flags=re.M)                             # tables
    t = re.sub(r"^#+\s*", "", t, flags=re.M)                                     # headings
    t = re.sub(r"[*_`>]+", "", t)                                                # emphasis, quotes
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)                                # bullets
    t = t.replace("~", "about ").replace("%", " percent").replace("&", " and ")
    t = re.sub(r"\$\s?(\d[\d,.]*)(\s*(?:thousand|million|billion|trillion))?", r"\1\2 dollars", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*[—–-]\s*([.,;:!?]|$)", r"\1", t)                              # dangling dashes
    t = re.sub(r"\s+([.,;:!?])", r"\1", t)
    sentences = re.split(r"(?<=[.!?])\s+", t)
    if len(sentences) > max_sentences:
        t = " ".join(sentences[:max_sentences]) + " The full details are on screen."
    return t


def tts_engine(piper_voice: str = "") -> str:
    """Which speech engine this machine will use."""
    if piper_voice and Path(piper_voice).expanduser().exists() and shutil.which("piper"):
        return "piper"
    if sys.platform == "darwin" and shutil.which("say"):
        return "say"
    if shutil.which("espeak-ng") or shutil.which("espeak"):
        return "espeak"
    try:
        __import__("pyttsx3")
        return "pyttsx3"
    except ImportError:
        return "none"


class Voice:
    def __init__(self, whisper_model: str = "base.en", piper_voice: str = "", say_voice: str = ""):
        self.whisper_model_name = whisper_model
        self.piper_voice = str(Path(piper_voice).expanduser()) if piper_voice else ""
        self.say_voice = say_voice
        self._whisper = None
        self._tts = None

    # ------------------------------------------------------------- listening
    def record(self, max_seconds: float = 20, silence_seconds: float = 1.2, threshold: float = 0.015):
        """Record from the default mic until the speaker pauses. Returns float32 mono audio."""
        np = _require("numpy")
        sd = _require("sounddevice")
        chunk = int(SAMPLE_RATE * CHUNK_SECONDS)
        frames, heard_speech, quiet = [], False, 0.0
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
            for _ in range(int(max_seconds / CHUNK_SECONDS)):
                data, _ = stream.read(chunk)
                frames.append(data[:, 0].copy())
                level = float(np.sqrt(np.mean(data ** 2)))
                if level > threshold:
                    heard_speech, quiet = True, 0.0
                elif heard_speech:
                    quiet += CHUNK_SECONDS
                    if quiet >= silence_seconds:
                        break
        return np.concatenate(frames) if frames else np.zeros(0, dtype="float32")

    def transcribe(self, audio) -> str:
        """``audio``: float32 numpy array at 16 kHz, or a path / bytes of a WAV file."""
        if self._whisper is None:
            fw = _require("faster_whisper")
            self._whisper = fw.WhisperModel(self.whisper_model_name, device="auto", compute_type="int8")
        if isinstance(audio, (str, Path)):
            audio = Path(audio).read_bytes()
        if isinstance(audio, (bytes, bytearray)):
            # WAV is decoded here (light, no ffmpeg); anything else (webm from a browser) goes to Whisper's decoder
            audio = wav_to_array(bytes(audio)) if bytes(audio[:4]) == b"RIFF" else io.BytesIO(bytes(audio))
        segments, _ = self._whisper.transcribe(audio, language="en", vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    def listen(self) -> str:
        return self.transcribe(self.record())

    # -------------------------------------------------------------- speaking
    @property
    def engine(self) -> str:
        return tts_engine(self.piper_voice)

    def say(self, text: str) -> None:
        text = speakable(text)
        if not text:
            return
        engine = self.engine
        if engine == "piper":
            self._play(self.synthesize(text))
        elif engine == "say":
            if subprocess.run(["say", *(["-v", self.say_voice] if self.say_voice else []), text],
                              capture_output=True).returncode and self.say_voice:
                subprocess.run(["say", text], check=False)  # that voice isn't installed: use the default
        elif engine == "espeak":
            subprocess.run([shutil.which("espeak-ng") or "espeak", "-s", "165", text], check=False,
                           capture_output=True)
        elif engine == "pyttsx3":
            if self._tts is None:
                self._tts = _require("pyttsx3").init()
            self._tts.say(text)
            self._tts.runAndWait()
        else:
            print(f"(no speech engine) {text}", file=sys.stderr)

    def synthesize(self, text: str) -> bytes:
        """Speech as WAV bytes (16-bit mono) — what a device like an ESP32 plays."""
        text = speakable(text)
        engine = self.engine
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "speech.wav"
            if engine == "piper":
                subprocess.run(["piper", "--model", self.piper_voice, "--output_file", str(out)],
                               input=text.encode(), check=True, capture_output=True)
            elif engine == "say":
                subprocess.run(["say", *(["-v", self.say_voice] if self.say_voice else []), "-o", str(out),
                                "--file-format=WAVE", "--data-format=LEI16@16000", text], check=True, capture_output=True)
            elif engine == "espeak":
                subprocess.run([shutil.which("espeak-ng") or "espeak", "-s", "165", "-w", str(out), text],
                               check=True, capture_output=True)
            else:
                raise VoiceUnavailable("no speech engine that can write audio: install Piper "
                                       "(pip install piper-tts) or espeak-ng")
            return out.read_bytes()

    def _play(self, wav: bytes) -> None:
        player = next((p for p in ("afplay", "aplay", "paplay") if shutil.which(p)), None)
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(wav)
            f.flush()
            if player:
                subprocess.run([player, f.name], check=False, capture_output=True)
                return
            sf, sd = _require("soundfile"), _require("sounddevice")
            audio, sr = sf.read(f.name)
            sd.play(audio, sr)
            sd.wait()


def wav_to_array(data: bytes):
    """WAV bytes → float32 mono at 16 kHz, without PyAV/ffmpeg (lighter, and fewer version clashes on a Pi)."""
    np = _require("numpy")
    with wave.open(io.BytesIO(data)) as w:
        ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise VoiceUnavailable("send 16-bit PCM WAV audio")
    audio = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    if rate != SAMPLE_RATE and len(audio):
        n = int(len(audio) * SAMPLE_RATE / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype("float32")
    return audio


def to_rate(wav: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """Resample a WAV to ``rate`` Hz, 16-bit mono (what an ESP32's I2S speaker plays most easily)."""
    np = _require("numpy")
    with wave.open(io.BytesIO(wav)) as w:
        if w.getframerate() == rate and w.getnchannels() == 1 and w.getsampwidth() == 2:
            return wav
        src_rate = w.getframerate()
    audio = wav_to_array(wav)  # → 16 kHz float32
    if rate != SAMPLE_RATE and len(audio):
        n = int(len(audio) * rate / SAMPLE_RATE)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    del src_rate
    return out.getvalue()


def wav_info(data: bytes) -> dict:
    """Basic facts about a WAV upload (and a check that it is one)."""
    with wave.open(io.BytesIO(data)) as w:
        return {"channels": w.getnchannels(), "rate": w.getframerate(), "seconds": round(w.getnframes() / w.getframerate(), 2)}
