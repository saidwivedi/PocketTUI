"""Tests for the voice transcription route.

The route's own logic — the size cap, the not-setup answers, the shape it hands
back — is tested with the binaries stubbed out, so it runs anywhere. The one
test that shells out to the real whisper build is skipped unless voice/ has been
populated, because it is the only thing that proves the pieces fit together.
"""

import array
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402
import resolver as R  # noqa: E402

BENCH_AUDIO = Path(
    "/tmp/claude-7560/-home-sdwivedi-work-pockettui/"
    "9710542c-4467-412c-b802-aeed18ab3059/scratchpad/asr_bench/audio"
)


def body(response):
    """The decoded JSON of a JSONResponse, which holds bytes rather than a dict."""
    import json
    return json.loads(response.body)


SHELL_SCREEN = [
    "sai@box:~/work/pockettui$ git status",
    "On branch main",
    "sai@box:~/work/pockettui$ ",
]


@pytest.fixture
def no_tmux(monkeypatch):
    """No tmux server: the pane sources are absent, as on a bare CI box."""
    monkeypatch.setattr(A, "tmux", lambda *a: (1, ""))
    R._cwd_cache.clear()


@pytest.fixture
def at_a_shell(monkeypatch, no_tmux):
    """A pane sitting at a shell prompt — the register the full rule table needs.

    Without this the register falls back to the conservative "claude", where
    standalone operators and most joiners are deliberately switched off, so a
    test of those rules would be asserting the wrong mode.
    """
    monkeypatch.setattr(A, "resolve_target", lambda s, d: "work")
    monkeypatch.setattr(A, "capture_pane", lambda name, lines=60: SHELL_SCREEN)
    monkeypatch.setattr(A, "pane_cwd", lambda name: "")


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A voice/ directory that looks complete without containing a real model."""
    binary = tmp_path / "whisper-cli"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    (tmp_path / "ggml-base.en.bin").write_text("not really a model")
    monkeypatch.setattr(A, "VOICE_DIR", tmp_path)
    monkeypatch.delenv("POCKETTUI_WHISPER_BIN", raising=False)
    monkeypatch.delenv("POCKETTUI_WHISPER_MODEL", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Asset discovery
# ---------------------------------------------------------------------------

def test_missing_voice_dir_reads_as_not_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "VOICE_DIR", tmp_path / "absent")
    monkeypatch.delenv("POCKETTUI_WHISPER_BIN", raising=False)
    assert A.whisper_paths() == (None, None)


def test_binary_without_a_model_reads_as_not_setup(tmp_path, monkeypatch):
    """A half-finished setup must not fail later, inside the subprocess."""
    binary = tmp_path / "whisper-cli"
    binary.write_text("")
    binary.chmod(0o755)
    monkeypatch.setattr(A, "VOICE_DIR", tmp_path)
    monkeypatch.delenv("POCKETTUI_WHISPER_BIN", raising=False)
    monkeypatch.delenv("POCKETTUI_WHISPER_MODEL", raising=False)
    assert A.whisper_paths() == (None, None)


def test_base_en_wins_when_several_models_are_present(installed, monkeypatch):
    (installed / "ggml-small.en.bin").write_text("")
    (installed / "ggml-tiny.en.bin").write_text("")
    _, model = A.whisper_paths()
    assert model.name == "ggml-base.en.bin"


def test_env_overrides_are_respected(installed, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere.bin"
    other.write_text("")
    monkeypatch.setenv("POCKETTUI_WHISPER_MODEL", str(other))
    _, model = A.whisper_paths()
    assert model == other


# ---------------------------------------------------------------------------
# Route logic
# ---------------------------------------------------------------------------

def test_not_setup_when_voice_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "VOICE_DIR", tmp_path / "absent")
    monkeypatch.delenv("POCKETTUI_WHISPER_BIN", raising=False)
    response = A.transcribe(b"audio bytes", "work", "phone")
    assert response.status_code == 503
    assert body(response) == {"error": "not_setup"}


def test_oversize_audio_is_refused_before_any_work(installed, monkeypatch):
    """The cap is checked first: nothing should spawn ffmpeg for 20 MB+."""
    def explode(*a, **k):
        raise AssertionError("no subprocess may run for an oversize body")

    monkeypatch.setattr(subprocess, "run", explode)
    response = A.transcribe(b"x" * (A.MAX_AUDIO_BYTES + 1), "work", "phone")
    assert response.status_code == 413
    assert body(response) == {"error": "audio_too_large"}


def test_empty_body_is_refused(installed):
    response = A.transcribe(b"", "work", "phone")
    assert response.status_code == 422
    assert body(response) == {"error": "empty_audio"}


def test_missing_ffmpeg_reads_as_not_setup(installed, monkeypatch):
    monkeypatch.setattr(A.shutil, "which", lambda name: None)
    response = A.transcribe(b"audio bytes", "work", "phone")
    assert response.status_code == 503
    assert body(response) == {"error": "no_ffmpeg"}


def test_undecodable_audio_is_refused(installed, monkeypatch, no_tmux):
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "undecodable_audio")
    response = A.transcribe(b"not audio at all", "work", "phone")
    assert response.status_code == 422
    assert body(response) == {"error": "undecodable_audio"}


def test_success_shape(installed, monkeypatch, at_a_shell):
    """text/raw/ms, with the resolver's repair in text and the transcript in raw."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(
        A, "run_whisper",
        lambda b, m, w, p: "pie test tests slash test underscore app dot py")

    response = A.transcribe(b"audio bytes", "work", "phone")
    assert response.status_code == 200
    payload = body(response)
    assert set(payload) == {"text", "raw", "ms"}
    assert payload["raw"] == "pie test tests slash test underscore app dot py"
    assert payload["text"] == "pytest tests/test_app.py"
    assert isinstance(payload["ms"], int) and payload["ms"] >= 0


def test_the_asr_rules_run_on_the_transcript(installed, monkeypatch,
                                             at_a_shell):
    """The route must ask for the ASR pass; without it the pipe stays a "pip"."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_whisper",
                        lambda b, m, w, p: "git status pip grep main")
    payload = body(A.transcribe(b"audio bytes", "work", "phone"))
    assert payload["text"] == "git status | grep main"


def test_silence_answers_empty_rather_than_failing(installed, monkeypatch, no_tmux):
    """whisper heard nothing: an empty compose bar, not an error the user sees."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_whisper", lambda b, m, w, p: "")
    response = A.transcribe(b"audio bytes", "work", "phone")
    assert response.status_code == 200
    assert body(response) == {"text": "", "raw": "", "ms": body(response)["ms"]}


def test_a_whisper_crash_answers_a_shape_not_a_traceback(installed, monkeypatch,
                                                         no_tmux):
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")

    def boom(*a, **k):
        raise RuntimeError("ggml exploded")

    monkeypatch.setattr(A, "run_whisper", boom)
    response = A.transcribe(b"audio bytes", "work", "phone")
    assert response.status_code == 500
    assert body(response) == {"error": "transcribe_failed"}


def test_a_whisper_timeout_answers_a_shape(installed, monkeypatch, no_tmux):
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")

    def stall(*a, **k):
        raise subprocess.TimeoutExpired("whisper-cli", A.WHISPER_TIMEOUT_S)

    monkeypatch.setattr(A, "run_whisper", stall)
    response = A.transcribe(b"audio bytes", "work", "phone")
    assert response.status_code == 500
    assert body(response) == {"error": "transcribe_timeout"}


# ---------------------------------------------------------------------------
# Silence gate
# ---------------------------------------------------------------------------

def write_wav(path, samples, rate=16000):
    """A 16 kHz mono s16 WAV — the shape decode_audio always hands the gate."""
    with wave.open(str(path), "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(array.array("h", samples).tobytes())
    return path


def tone(seconds, amplitude, rate=16000, freq=440.0):
    return [int(amplitude * math.sin(2 * math.pi * freq * n / rate))
            for n in range(int(rate * seconds))]


def test_digital_silence_is_gated(tmp_path):
    wav = write_wav(tmp_path / "silent.wav", [0] * 16000 * 2)
    assert A.is_silent(wav)


def test_room_tone_is_gated(tmp_path):
    """A hot mic in a quiet room is not speech, and whisper will invent some."""
    hiss = [int(120 * math.sin(n * 0.7)) for n in range(16000 * 2)]
    assert A.is_silent(write_wav(tmp_path / "hiss.wav", hiss))


def test_a_clip_too_short_to_hold_a_word_is_gated(tmp_path):
    wav = write_wav(tmp_path / "tap.wav", tone(0.2, 12000))
    assert A.is_silent(wav)


def test_speech_level_audio_passes(tmp_path):
    wav = write_wav(tmp_path / "loud.wav", tone(2.0, 12000))
    assert not A.is_silent(wav)


def test_a_quiet_talker_still_passes(tmp_path):
    """The gate must be well clear of soft speech — a false gate loses words."""
    wav = write_wav(tmp_path / "quiet.wav", tone(2.0, 1500))
    assert not A.is_silent(wav)


def test_one_word_in_a_long_silence_passes(tmp_path):
    """Peak frame RMS, not the average: a short word must keep the whole clip."""
    samples = [0] * 16000 * 3 + tone(0.4, 9000) + [0] * 16000 * 3
    assert not A.is_silent(write_wav(tmp_path / "word.wav", samples))


def test_unreadable_audio_is_not_treated_as_silence(tmp_path):
    """Whatever this cannot parse goes to whisper rather than being dropped."""
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"RIFF not actually a wave file")
    assert not A.is_silent(junk)


@pytest.mark.skipif(not (BENCH_AUDIO / "p01.wav").exists(),
                    reason="benchmark audio is not present")
def test_real_speech_is_never_gated():
    """The clips the benchmark calls speech must all reach the model."""
    for name in ("p01.wav", "p01_noisy.wav", "g01.wav"):
        clip = BENCH_AUDIO / name
        if clip.exists():
            assert not A.is_silent(clip), f"{name} was gated as silence"


def test_silence_skips_whisper_entirely(installed, monkeypatch, no_tmux):
    """The whole point: no transcription call, and ms reported as 0."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "is_silent", lambda wav: True)

    def explode(*a, **k):
        raise AssertionError("whisper must not run on a silent clip")

    monkeypatch.setattr(A, "run_whisper", explode)
    response = A.transcribe(b"audio bytes", "work", "phone")
    assert response.status_code == 200
    assert body(response) == {"text": "", "raw": "", "ms": 0}


# ---------------------------------------------------------------------------
# decode_audio: fragmented-mp4 (iOS) regression
# ---------------------------------------------------------------------------
# Root cause, proven live: iOS MediaRecorder uploads fragmented mp4
# (audio/mp4; codecs=mp4a.40.2) whose per-fragment timestamps restart at
# zero. ffmpeg's mp4 demuxer then decodes only the first fragment (~1s of a
# 4.5s recording) instead of the whole thing. These fixtures are built with
# ffmpeg itself rather than shipped as binary blobs.

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")


def _make_test_tone_wav(path: Path, seconds: float, rate: int = 16000) -> Path:
    """A plain (non-fragmented) WAV — ffmpeg's own encoder input for the fixtures."""
    return write_wav(path, tone(seconds, 12000, rate=rate), rate=rate)


def _encode_fragmented_mp4(src_wav: Path, dst_mp4: Path) -> None:
    """AAC-in-mp4 with per-fragment timestamps that restart at zero.

    This is the exact shape iOS MediaRecorder produces and the exact shape
    that breaks ffmpeg's mp4 demuxer: frag_keyframe+empty_moov (movfrag)
    writes each fragment as if it were its own presentation, so a naive
    `-i in.mp4` decode of the whole file only surfaces the first fragment.
    """
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(src_wav),
         "-c:a", "aac", "-b:a", "64k",
         "-movflags", "frag_keyframe+empty_moov+default_base_moof",
         "-f", "mp4", str(dst_mp4)],
        check=True, capture_output=True, timeout=A.FFMPEG_TIMEOUT_S)


def _encode_webm_opus(src_wav: Path, dst_webm: Path) -> None:
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(src_wav),
         "-c:a", "libopus", "-f", "webm", str(dst_webm)],
        check=True, capture_output=True, timeout=A.FFMPEG_TIMEOUT_S)


def _wav_duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as clip:
        return clip.getnframes() / (clip.getframerate() or 16000)


@needs_ffmpeg
def test_fragmented_mp4_decodes_in_full_via_adts(tmp_path):
    """The regression itself: a direct mp4 decode would truncate to ~1s.

    Encoding fragmented (frag_keyframe+empty_moov+default_base_moof) is what
    makes each fragment's timestamps restart at zero, which is what the ADTS
    extraction path (-c copy -f adts) exists to bypass.
    """
    src_wav = _make_test_tone_wav(tmp_path / "src.wav", 4.0)
    frag_mp4 = tmp_path / "frag.mp4"
    _encode_fragmented_mp4(src_wav, frag_mp4)

    out_wav = tmp_path / "out.wav"
    error = A.decode_audio(frag_mp4.read_bytes(), out_wav,
                          "audio/mp4; codecs=mp4a.40.2")
    assert error == ""
    assert _wav_duration_s(out_wav) > 3.0, (
        "decoded only the first fragment — the ADTS path did not take effect")


@needs_ffmpeg
def test_webm_opus_still_decodes_via_the_direct_path(tmp_path):
    """Non-mp4 content types must skip the ADTS extraction attempt entirely."""
    src_wav = _make_test_tone_wav(tmp_path / "src.wav", 2.0)
    webm = tmp_path / "clip.webm"
    _encode_webm_opus(src_wav, webm)

    out_wav = tmp_path / "out.wav"
    error = A.decode_audio(webm.read_bytes(), out_wav, "audio/webm; codecs=opus")
    assert error == ""
    assert _wav_duration_s(out_wav) > 1.5


@needs_ffmpeg
def test_adts_extraction_failure_falls_back_to_direct_decode(tmp_path, monkeypatch):
    """A content-type of mp4 that fails ADTS extraction must not be lost."""
    src_wav = _make_test_tone_wav(tmp_path / "src.wav", 2.0)
    frag_mp4 = tmp_path / "frag.mp4"
    _encode_fragmented_mp4(src_wav, frag_mp4)

    monkeypatch.setattr(A, "_extract_aac", lambda src, aac: False)
    out_wav = tmp_path / "out.wav"
    error = A.decode_audio(frag_mp4.read_bytes(), out_wav,
                          "audio/mp4; codecs=mp4a.40.2")
    assert error == ""
    assert _wav_duration_s(out_wav) > 0


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def test_prompt_names_the_words_on_screen(no_tmux):
    prompt = A.transcribe_prompt(["$ pytest tests/test_camerahmr.py"], "")
    assert prompt.startswith("Terminal session. Commands and files: ")
    assert "camerahmr" in prompt or "test_camerahmr.py" in prompt


def test_prompt_is_capped(no_tmux):
    """A long scrollback must not push the prompt past whisper's useful window."""
    screen = [f"identifier_number_{n} " * 8 for n in range(400)]
    assert len(A.transcribe_prompt(screen, "")) <= A.MAX_PROMPT_CHARS


def test_prompt_has_no_duplicates(no_tmux):
    prompt = A.transcribe_prompt(["pytest pytest pytest resolver resolver"], "")
    vocab = prompt.split(": ", 1)[1].rstrip(".").split(", ")
    assert len(vocab) == len(set(v.lower() for v in vocab))


def test_prompt_is_empty_without_any_vocabulary(no_tmux):
    """Nothing worth saying means no --prompt at all, not an empty sentence."""
    assert A.transcribe_prompt([], "") == ""


# ---------------------------------------------------------------------------
# The real thing
# ---------------------------------------------------------------------------

@pytest.mark.skipif(A.whisper_paths()[0] is None,
                    reason="voice/ is not set up (run setup_voice.sh)")
@pytest.mark.skipif(not (BENCH_AUDIO / "p01.wav").exists(),
                    reason="benchmark audio is not present")
def test_real_audio_transcribes_and_resolves(tmp_path, monkeypatch):
    """End to end through the actual binary: spoken audio in, a filename out.

    p01 is the benchmark's "run pytest on tests slash test underscore camera h m
    r dot py". base.en writes the path as "test/test_camerahmor.py" or similar;
    what has to survive this pipeline is `tests/test_camerahmr.py` — the exact
    file, spelled the way the filesystem spells it, which the model had no way
    to know and only the cwd index can supply.

    The command word is deliberately NOT asserted. base.en renders it as
    "pipest"/"pitus"/"Pitast" run to run, and because whisper tends to prefix a
    spoken "run", the word lands mid-sentence where the $PATH-entries-only-at-
    command-position guard refuses to touch it. That guard is the conservatism
    this resolver is built on, so the test documents it rather than fighting it.
    """
    # A project the resolver can see, so the snap has the real filename to reach
    # for — exactly what a user dictating into their own repo would have.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_camerahmr.py").write_text("")
    (tmp_path / "app.py").write_text("")
    R._cwd_cache.clear()

    monkeypatch.setattr(A, "tmux", lambda *a: (1, ""))
    monkeypatch.setattr(A, "resolve_target", lambda s, d: "work")
    monkeypatch.setattr(A, "capture_pane", lambda name, lines=60: [
        "sai@box:~/work$ ls tests/",
        "test_camerahmr.py",
        "sai@box:~/work$ ",
    ])
    monkeypatch.setattr(A, "pane_cwd", lambda name: str(tmp_path))

    response = A.transcribe((BENCH_AUDIO / "p01.wav").read_bytes(), "work", "phone")
    assert response.status_code == 200
    payload = body(response)

    assert payload["raw"], "the real binary produced no transcript at all"
    # The repair the pipeline exists to make: the model's "test/" becomes the
    # directory that is actually there, with the filename spelled correctly.
    assert "tests/test_camerahmr.py" in payload["text"]
    assert payload["ms"] > 0
