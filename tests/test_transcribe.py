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


@pytest.fixture(autouse=True)
def no_engine_override(monkeypatch):
    """No env-forced engine and no real Parakeet model, whatever the shell set.

    Every test below that does not say otherwise means "the whisper path", and
    that only holds if the machine running the suite is not itself pointing at a
    Parakeet install through the environment.
    """
    monkeypatch.delenv("POCKETTUI_VOICE_ENGINE", raising=False)
    monkeypatch.delenv("POCKETTUI_PARAKEET_MODEL", raising=False)
    monkeypatch.setattr(A, "PARAKEET_DIR", Path("/nonexistent-parakeet"))


@pytest.fixture(autouse=True)
def no_hotword_username(monkeypatch):
    """No login name in the hotword list, whoever is running the suite.

    The list reserves the username unconditionally, so leaving the real one in
    place would make every exact-match assertion about hotwords depend on the
    account the suite ran under. The tests that are about the reservation put a
    name back.
    """
    monkeypatch.setattr(A, "_hotword_username", lambda: "")


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


@pytest.fixture
def parakeet_installed(tmp_path, monkeypatch):
    """A Parakeet model directory holding the four files the probe looks for.

    The contents are not models — every test using this stubs the recognizer
    out. What it proves is the discovery and selection logic, which is the only
    part of the engine that runs on a machine with no sherpa-onnx.
    """
    model = tmp_path / "parakeet" / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
    model.mkdir(parents=True)
    for name in A.PARAKEET_FILES:
        (model / name).write_text("▁t 1\n▁th 2\n" if name == "tokens.txt" else "x")
    monkeypatch.setattr(A, "VOICE_DIR", tmp_path)
    monkeypatch.setattr(A, "PARAKEET_DIR", tmp_path / "parakeet")
    return model


@pytest.fixture
def sherpa_importable(monkeypatch):
    """sherpa-onnx present, whether or not this machine really has it."""
    import types
    monkeypatch.setitem(sys.modules, "sherpa_onnx", types.ModuleType("sherpa_onnx"))


@pytest.fixture(autouse=True)
def fresh_parakeet_state(monkeypatch):
    """The engine's process-lifetime globals, reset around every test.

    The dead flag is deliberately permanent in the server — a wedged decode
    retires Parakeet until a restart — so a test that trips it would otherwise
    take every later test's Parakeet down with it. The recognizer and its worker
    are reset for the same reason a real restart resets them: they are keyed to
    a model directory that each test invents afresh in tmp_path.
    """
    monkeypatch.setattr(A, "_parakeet_recognizer", None)
    monkeypatch.setattr(A, "_parakeet_pool", None)
    monkeypatch.setattr(A, "_parakeet_dead", False)


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
# Engine selection
# ---------------------------------------------------------------------------
# The Parakeet half is stubbed at the sherpa-onnx boundary: what these prove is
# which engine a given install picks, not what either engine decodes.

def test_parakeet_is_discovered_by_glob(parakeet_installed, sherpa_importable):
    """A tarball unpacked into voice/parakeet/ is found without configuration."""
    assert A.parakeet_model_dir() == parakeet_installed
    assert A.parakeet_available()


def test_v2_wins_when_several_models_are_present(parakeet_installed):
    """The base.en rule, applied to Parakeet: prefer the English build by name.

    v3 sorts above v2 and is the newer release, so without an explicit
    preference it would win — and it is multilingual, measurably worse on the
    English this product transcribes. Someone who wants it names it in
    POCKETTUI_PARAKEET_MODEL.
    """
    for version in ("v1", "v3"):
        other = parakeet_installed.parent / f"sherpa-onnx-nemo-parakeet-tdt-0.6b-{version}-int8"
        other.mkdir()
        for name in A.PARAKEET_FILES:
            (other / name).write_text("x")
    assert A.parakeet_model_dir() == parakeet_installed


def test_the_newest_wins_when_there_is_no_v2(parakeet_installed):
    """No English build to prefer: fall back to the newest of what is there."""
    import shutil as sh
    sh.rmtree(parakeet_installed)
    for version in ("v3", "v4"):
        other = parakeet_installed.parent / f"sherpa-onnx-nemo-parakeet-tdt-0.6b-{version}-int8"
        other.mkdir()
        for name in A.PARAKEET_FILES:
            (other / name).write_text("x")
    assert A.parakeet_model_dir().name.endswith("v4-int8")


def test_an_incomplete_v2_does_not_shadow_a_working_model(parakeet_installed):
    """Preference applies among usable models, not ahead of usability."""
    (parakeet_installed / "joiner.int8.onnx").unlink()
    other = parakeet_installed.parent / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
    other.mkdir()
    for name in A.PARAKEET_FILES:
        (other / name).write_text("x")
    assert A.parakeet_model_dir() == other


def test_a_half_unpacked_model_reads_as_absent(parakeet_installed,
                                               sherpa_importable):
    """Same rule whisper_paths() follows: fail the probe, not the decode."""
    (parakeet_installed / "joiner.int8.onnx").unlink()
    assert A.parakeet_model_dir() is None
    assert not A.parakeet_available()


def test_parakeet_model_env_override(parakeet_installed, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for name in A.PARAKEET_FILES:
        (elsewhere / name).write_text("x")
    monkeypatch.setenv("POCKETTUI_PARAKEET_MODEL", str(elsewhere))
    assert A.parakeet_model_dir() == elsewhere


def test_parakeet_env_override_pointing_nowhere_is_absent(parakeet_installed,
                                                          tmp_path, monkeypatch):
    """An override is a statement, not a hint: it does not fall back to the glob."""
    monkeypatch.setenv("POCKETTUI_PARAKEET_MODEL", str(tmp_path / "absent"))
    assert A.parakeet_model_dir() is None


def test_parakeet_is_preferred_when_both_are_installed(installed,
                                                       parakeet_installed,
                                                       sherpa_importable):
    assert A.voice_engine() == "parakeet"


def test_a_missing_model_falls_back_to_whisper(installed, sherpa_importable):
    """sherpa-onnx installed but no model downloaded: whisper still answers."""
    assert A.parakeet_model_dir() is None
    assert A.voice_engine() == "whisper"


def test_a_missing_sherpa_falls_back_to_whisper(installed, parakeet_installed,
                                                monkeypatch):
    """The model is on disk but the wheel is not: whisper, not a 503."""
    monkeypatch.setattr(A, "parakeet_available", lambda: False)
    # whisper_paths() reads VOICE_DIR, which parakeet_installed has repointed.
    monkeypatch.setenv("POCKETTUI_WHISPER_BIN", str(installed / "whisper-cli"))
    monkeypatch.setenv("POCKETTUI_WHISPER_MODEL", str(installed / "ggml-base.en.bin"))
    assert A.voice_engine() == "whisper"


def test_env_can_force_either_engine(installed, parakeet_installed,
                                     sherpa_importable, monkeypatch):
    monkeypatch.setenv("POCKETTUI_WHISPER_BIN", str(installed / "whisper-cli"))
    monkeypatch.setenv("POCKETTUI_WHISPER_MODEL", str(installed / "ggml-base.en.bin"))
    monkeypatch.setenv("POCKETTUI_VOICE_ENGINE", "whisper")
    assert A.voice_engine() == "whisper"
    monkeypatch.setenv("POCKETTUI_VOICE_ENGINE", "parakeet")
    assert A.voice_engine() == "parakeet"


def test_forcing_an_engine_that_is_absent_is_not_a_silent_fallback(
        installed, monkeypatch):
    """Pinned to Parakeet with no Parakeet: not_setup, not a quiet whisper run."""
    monkeypatch.setenv("POCKETTUI_VOICE_ENGINE", "parakeet")
    assert A.voice_engine() == ""


def test_neither_engine_is_the_only_not_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "VOICE_DIR", tmp_path / "absent")
    monkeypatch.delenv("POCKETTUI_WHISPER_BIN", raising=False)
    assert A.voice_engine() == ""


def test_a_request_can_ask_for_either_installed_engine(installed,
                                                       parakeet_installed,
                                                       sherpa_importable,
                                                       monkeypatch):
    """The per-request choice, where the env has not pinned one."""
    monkeypatch.setenv("POCKETTUI_WHISPER_BIN", str(installed / "whisper-cli"))
    monkeypatch.setenv("POCKETTUI_WHISPER_MODEL", str(installed / "ggml-base.en.bin"))
    assert A.voice_engine("whisper") == "whisper"
    assert A.voice_engine("parakeet") == "parakeet"


def test_the_env_outranks_what_the_request_asked_for(installed,
                                                     parakeet_installed,
                                                     sherpa_importable,
                                                     monkeypatch):
    """The operator's rollback has to survive a phone that disagrees with it."""
    monkeypatch.setenv("POCKETTUI_WHISPER_BIN", str(installed / "whisper-cli"))
    monkeypatch.setenv("POCKETTUI_WHISPER_MODEL", str(installed / "ggml-base.en.bin"))
    monkeypatch.setenv("POCKETTUI_VOICE_ENGINE", "whisper")
    assert A.voice_engine("parakeet") == "whisper"


def test_asking_for_an_absent_engine_is_not_a_silent_fallback(installed):
    """The client owns the fallback, so it must be told, not quietly re-routed."""
    assert A.voice_engine("parakeet") == ""
    assert A.voice_engine() == "whisper"


def test_an_unknown_requested_engine_reads_as_no_request(installed):
    assert A.voice_engine("nonesuch") == "whisper"


# ---------------------------------------------------------------------------
# Voice status
# ---------------------------------------------------------------------------

def test_voice_status_names_both_engines(installed, parakeet_installed,
                                         sherpa_importable, monkeypatch):
    monkeypatch.setenv("POCKETTUI_WHISPER_BIN", str(installed / "whisper-cli"))
    monkeypatch.setenv("POCKETTUI_WHISPER_MODEL", str(installed / "ggml-base.en.bin"))
    assert body(A.api_voice_status()) == {
        "engines": {"parakeet": True, "whisper": True}, "active": "parakeet"}


def test_voice_status_with_only_whisper(installed):
    assert body(A.api_voice_status()) == {
        "engines": {"parakeet": False, "whisper": True}, "active": "whisper"}


def test_voice_status_with_neither_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "VOICE_DIR", tmp_path / "absent")
    monkeypatch.delenv("POCKETTUI_WHISPER_BIN", raising=False)
    assert body(A.api_voice_status()) == {
        "engines": {"parakeet": False, "whisper": False}, "active": ""}


def test_voice_status_reflects_the_env_force(installed, parakeet_installed,
                                             sherpa_importable, monkeypatch):
    """Both installed, one pinned: the picker has to show which one wins."""
    monkeypatch.setenv("POCKETTUI_WHISPER_BIN", str(installed / "whisper-cli"))
    monkeypatch.setenv("POCKETTUI_WHISPER_MODEL", str(installed / "ggml-base.en.bin"))
    monkeypatch.setenv("POCKETTUI_VOICE_ENGINE", "whisper")
    payload = body(A.api_voice_status())
    assert payload["engines"] == {"parakeet": True, "whisper": True}
    assert payload["active"] == "whisper"


# ---------------------------------------------------------------------------
# The bpe vocabulary sherpa-onnx will not ship
# ---------------------------------------------------------------------------

def test_bpe_vocab_is_synthesized_from_tokens(parakeet_installed):
    vocab = A.parakeet_bpe_vocab(parakeet_installed)
    assert vocab.read_text().splitlines() == ["▁t\t-1.0", "▁th\t-1.0"]


def test_bpe_vocab_keeps_pieces_that_contain_a_space(tmp_path):
    """Only the trailing id is one field, so the split has to come from the right."""
    tokens = tmp_path / "tokens.txt"
    tokens.write_text("<unk> 0\n  1\n▁a 2\n")
    assert A._bpe_vocab_text(tokens).splitlines() == ["<unk>\t-1.0", " \t-1.0",
                                                      "▁a\t-1.0"]


def test_an_existing_bpe_vocab_is_left_alone(parakeet_installed):
    vocab = parakeet_installed / "bpe.vocab"
    vocab.write_text("mine\t-1.0\n")
    assert A.parakeet_bpe_vocab(parakeet_installed).read_text() == "mine\t-1.0\n"


def test_a_read_only_model_directory_still_gets_a_vocab(parakeet_installed,
                                                        tmp_path, monkeypatch):
    """A model tree mounted read-only must run, not fail at recognizer build."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(A.os, "access", lambda path, mode: False)
    vocab = A.parakeet_bpe_vocab(parakeet_installed)
    assert vocab.parent == tmp_path / "cache" / "pockettui"
    assert vocab.read_text().splitlines() == ["▁t\t-1.0", "▁th\t-1.0"]
    assert not (parakeet_installed / "bpe.vocab").exists()


def test_the_bpe_vocab_is_never_passed_empty(parakeet_installed, monkeypatch):
    """The segfault guard: sherpa-onnx 1.13.6 dies on modeling_unit=bpe with an
    empty bpe_vocab, so the two must be passed together and the vocab non-empty.

    Asserted at the boundary rather than by inspection, because the failure mode
    is a process death that no exception handler downstream could report.
    """
    import types
    seen = {}

    def from_transducer(**kwargs):
        seen.update(kwargs)
        return object()

    fake = types.ModuleType("sherpa_onnx")
    fake.OfflineRecognizer = types.SimpleNamespace(from_transducer=from_transducer)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    monkeypatch.setattr(A, "_parakeet_recognizer", None)

    A.parakeet_recognizer(parakeet_installed)
    assert seen["modeling_unit"] == "bpe"
    assert seen["bpe_vocab"] and Path(seen["bpe_vocab"]).stat().st_size > 0
    # Hotwords are the reason for the vocab, and only this method takes them.
    assert seen["decoding_method"] == "modified_beam_search"


def test_the_recognizer_is_built_once_and_kept(parakeet_installed, monkeypatch):
    """~1.8 s to build and nothing to hold: a long-lived server builds it once."""
    import types
    builds = []

    fake = types.ModuleType("sherpa_onnx")
    fake.OfflineRecognizer = types.SimpleNamespace(
        from_transducer=lambda **kw: builds.append(kw) or object())
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    monkeypatch.setattr(A, "_parakeet_recognizer", None)

    first = A.parakeet_recognizer(parakeet_installed)
    assert A.parakeet_recognizer(parakeet_installed) is first
    assert len(builds) == 1


# ---------------------------------------------------------------------------
# Hotwords — the vocabulary channel Parakeet has in place of a prompt
# ---------------------------------------------------------------------------

def lines_of(text):
    """The bare words of a hotwords string, with the per-line score dropped."""
    return [line.rsplit(" :", 1)[0] for line in text.splitlines()]


def test_learned_words_come_before_history(monkeypatch):
    """The user corrected these by hand: they outrank anything merely typed."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    text = A.parakeet_hotwords(history=["micromamba"], learned=["tokenhmr"])
    assert lines_of(text) == ["tokenhmr", "micromamba"]


def test_the_vocabulary_is_deduplicated_case_insensitively(monkeypatch):
    """A word the user both typed and taught buys one slot, not two."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    text = A.parakeet_hotwords(history=["CameraHMR", "camerahmr", "sbatch"],
                               learned=["camerahmr"])
    assert lines_of(text) == ["camerahmr", "sbatch"]


def test_the_vocabulary_is_capped(monkeypatch):
    """500 words cost nothing to decode; the cap is what keeps that true."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    text = A.parakeet_hotwords(history=[f"hmr{n}x" for n in range(900)])
    assert len(text.splitlines()) == A.MAX_HOTWORDS


def test_learned_words_survive_the_cap(monkeypatch):
    """Ordering is what makes the cap safe: the best evidence is never cut."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    text = A.parakeet_hotwords(history=[f"hmr{n}x" for n in range(900)],
                               learned=["tokenhmr"])
    assert lines_of(text)[0] == "tokenhmr"
    assert len(text.splitlines()) == A.MAX_HOTWORDS


def test_the_cap_is_filled_with_what_survives_the_filters(monkeypatch):
    """A dropped word does not spend a slot — it is skipped, not blanked.

    The filters run before the cap is counted against, so a history full of
    English and of one generated family still fills all 500 slots from the
    words that earned them.
    """
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    history = ["and", "the", "here"] + [f"LINE_{n}" for n in range(60)] \
        + [f"hmr{n}x" for n in range(900)]
    lines = lines_of(A.parakeet_hotwords(history=history))
    assert len(lines) == A.MAX_HOTWORDS
    assert lines[:2] == ["LINE_0", "hmr0x"]
    assert not {"and", "the", "here"} & set(lines)


def test_a_short_vocabulary_is_exactly_what_survives(monkeypatch):
    """Below the cap there is nothing to rank: the survivors are the list."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    text = A.parakeet_hotwords(
        history=["and", "sbatch", "LINE_1", "LINE_2", "sbatch", "micromamba"])
    assert lines_of(text) == ["sbatch", "LINE_1", "micromamba"]


def test_common_english_is_dropped_from_history_but_not_from_learned(monkeypatch):
    """Parakeet writes "and" correctly unaided, so a slot spent on it is lost —
    unless the user taught it, which is evidence no shape test can outvote."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    assert lines_of(A.parakeet_hotwords(
        history=["and", "here", "this", "tokenhmr"])) == ["tokenhmr"]
    assert lines_of(A.parakeet_hotwords(
        history=["tokenhmr"], learned=["and", "here"])) \
        == ["and", "here", "tokenhmr"]


def test_a_generated_family_buys_one_slot_not_sixty(monkeypatch):
    """One `echo LINE_1 … LINE_60` in the scrollback is one word the user
    typed, not sixty words worth boosting."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    text = A.parakeet_hotwords(
        history=["LINE_3", "LINE_1", "LINE_2", "global_step500",
                 "global_step2000"])
    assert lines_of(text) == ["LINE_3", "global_step500"]


def test_different_stems_are_not_one_family(monkeypatch):
    """The family key is the stem, so two names that merely both end in a
    digit stay two names."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    text = A.parakeet_hotwords(history=["tokenhmr2", "camerahmr2", "v1", "v2"])
    assert lines_of(text) == ["tokenhmr2", "camerahmr2", "v1", "v2"]


def test_a_word_without_digits_is_left_alone(monkeypatch):
    """Nothing to strip is nothing to collapse: a digitless word passes through
    as itself, still subject to the English filter like any other."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    text = A.parakeet_hotwords(history=["micromamba", "sbatch", "report"])
    assert lines_of(text) == ["micromamba", "sbatch"]


def test_paths_are_split_on_the_separator_sherpa_treats_as_one(monkeypatch):
    """"/" is a hotword separator in sherpa-onnx, not a character in a word.

    Handed a path whole it would silently become several hotwords anyway, so
    it is split here where the budget can see and count the pieces.
    """
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    text = A.parakeet_hotwords(history=["tokenhmr/pockettui/app.py"])
    assert lines_of(text) == ["tokenhmr", "pockettui", "app.py"]


def test_words_the_encoder_cannot_segment_are_dropped(monkeypatch):
    """Non-ASCII encodes to an out-of-vocabulary token sherpa-onnx discards.

    Dropped here instead, where it costs a budget slot rather than a log line —
    and with it everything else that cannot be a single scored hotword: bare
    punctuation, a one-character word, and anything carrying the ":" that
    leads sherpa's own score syntax.
    """
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    text = A.parakeet_hotwords(
        history=["café", "七", "-", "x", "12345", "host:22", "sbatch"])
    assert lines_of(text) == ["sbatch"]


def test_every_line_carries_a_score_or_none_would(monkeypatch):
    """sherpa-onnx fills a missing score from a default nobody here chose, so
    the list is scored throughout rather than in part."""
    monkeypatch.setenv("POCKETTUI_HOTWORD_SCORE", "0.75")
    text = A.parakeet_hotwords(history=["micromamba", "sbatch"])
    assert text == "micromamba :0.75\nsbatch :0.75"


def test_an_empty_vocabulary_is_an_empty_string(monkeypatch):
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    assert A.parakeet_hotwords(history=[], learned=[]) == ""
    assert A.parakeet_hotwords() == ""


def test_the_username_is_in_the_vocabulary_whatever_else_is(monkeypatch):
    """Home and project paths are dictated constantly, and the username is the
    one word every one of them ends up needing. Before this it rode along only
    when the scrollback happened to hold it."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    monkeypatch.setattr(A, "_hotword_username", lambda: "sdwivedi")
    assert lines_of(A.parakeet_hotwords()) == ["sdwivedi"]
    assert lines_of(A.parakeet_hotwords(history=[], learned=[])) == ["sdwivedi"]


def test_the_username_sits_behind_the_learned_words_and_ahead_of_history(
        monkeypatch):
    """Learned words are still the strongest evidence here; everything the user
    merely typed is behind the one word they cannot do without."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    monkeypatch.setattr(A, "_hotword_username", lambda: "sdwivedi")
    text = A.parakeet_hotwords(history=["micromamba"], learned=["tokenhmr"])
    assert lines_of(text) == ["tokenhmr", "sdwivedi", "micromamba"]


def test_the_username_buys_one_slot_even_when_history_has_it(monkeypatch):
    """A user whose own name is all over their history reserves it once."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    monkeypatch.setattr(A, "_hotword_username", lambda: "sdwivedi")
    text = A.parakeet_hotwords(history=["sdwivedi", "sbatch", "sdwivedi"])
    assert lines_of(text) == ["sdwivedi", "sbatch"]


def test_the_username_survives_a_history_that_fills_the_cap(monkeypatch):
    """The reservation is what makes it a guarantee: 900 typed words cannot
    push it out of the 500 slots."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    monkeypatch.setattr(A, "_hotword_username", lambda: "sdwivedi")
    lines = lines_of(A.parakeet_hotwords(history=[f"hmr{n}x" for n in range(900)]))
    assert lines[0] == "sdwivedi"
    assert len(lines) == A.MAX_HOTWORDS


def test_the_username_bypasses_the_filters_history_words_face(monkeypatch):
    """A login name is a name, and no shape test would know that: "local" as a
    username is the word the decoder needs, not the English one it drops."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    monkeypatch.setattr(A, "_hotword_username", lambda: "local")
    assert lines_of(A.parakeet_hotwords(history=["local"])) == ["local"]


def test_a_login_with_no_readable_name_costs_one_word_not_the_decode(
        monkeypatch):
    """getuser() reads passwd and the environment; both can fail on a machine
    the server is otherwise perfectly able to transcribe on."""
    monkeypatch.setattr(A.getpass, "getuser",
                        lambda: (_ for _ in ()).throw(OSError("no passwd")))
    assert A._hotword_username() == ""


def test_the_boost_cannot_reach_the_cliff(monkeypatch):
    """Above ~1.5 the decoder repeats hotwords back at the user instead of
    transcribing. Confident nonsense is worse than an error, so the ceiling is
    structural rather than documented: no configuration can cross it."""
    for configured in ("1.6", "3.0", "999"):
        monkeypatch.setenv("POCKETTUI_HOTWORD_SCORE", configured)
        assert A.hotword_score() == A.HOTWORD_SCORE_MAX
    monkeypatch.setenv("POCKETTUI_HOTWORD_SCORE", "-2")
    assert A.hotword_score() == 0.0


def test_an_unparseable_boost_reads_as_unset(monkeypatch):
    """A malformed number in the environment must not cost a user dictation."""
    monkeypatch.setenv("POCKETTUI_HOTWORD_SCORE", "loud")
    assert A.hotword_score() == A.HOTWORD_SCORE_DEFAULT
    monkeypatch.setenv("POCKETTUI_HOTWORD_SCORE", "")
    assert A.hotword_score() == A.HOTWORD_SCORE_DEFAULT


def test_the_default_boost_is_the_measured_one(monkeypatch):
    """0.5: the top of the band that leaves the 42-clip benchmark unchanged."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    assert A.hotword_score() == 0.5
    assert A.HOTWORD_SCORE_DEFAULT < A.HOTWORD_SCORE_MAX


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


def test_the_parakeet_route_answers_the_same_shape(parakeet_installed,
                                                   sherpa_importable,
                                                   monkeypatch, at_a_shell):
    """Different engine, identical contract: the resolver's repair in text."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(
        A, "run_parakeet",
        lambda d, w, hotwords=None: ("pie test tests slash test underscore app dot py", None))

    def explode(*a, **k):
        raise AssertionError("whisper must not run when Parakeet is selected")

    monkeypatch.setattr(A, "run_whisper", explode)
    payload = body(A.transcribe(b"audio bytes", "work", "phone"))
    assert set(payload) == {"text", "raw", "ms"}
    assert payload["raw"] == "pie test tests slash test underscore app dot py"
    assert payload["text"] == "pytest tests/test_app.py"


def test_a_surviving_doubted_word_is_flagged(parakeet_installed, sherpa_importable,
                                             monkeypatch, at_a_shell):
    """A low-confidence word that reaches the final text ends up in `unsure`."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(
        A, "run_parakeet",
        lambda d, w, hotwords=None: ("hello world", {"hello": 0.3, "world": 0.95}))

    payload = body(A.transcribe(b"audio bytes", "work", "phone"))
    assert "unsure" in payload
    assert payload["unsure"] == ["hello"]


def test_no_unsure_key_when_everything_is_confident(parakeet_installed,
                                                     sherpa_importable, monkeypatch,
                                                     at_a_shell):
    """All confidences comfortably above ASR_CONF_LOW: no `unsure` key at all."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(
        A, "run_parakeet",
        lambda d, w, hotwords=None: ("hello world", {"hello": 0.95, "world": 0.95}))

    payload = body(A.transcribe(b"audio bytes", "work", "phone"))
    assert "unsure" not in payload


def test_no_unsure_key_on_the_whisper_path(installed, monkeypatch, at_a_shell):
    """Whisper never reports confidence, so `unsure` cannot be computed there."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_whisper", lambda b, m, w, p: "hello world")

    payload = body(A.transcribe(b"audio bytes", "work", "phone"))
    assert "unsure" not in payload


def test_a_doubted_word_the_resolver_rewrote_is_not_flagged(parakeet_installed,
                                                             sherpa_importable,
                                                             monkeypatch, at_a_shell):
    """Doubt the resolver already erased must not resurface in `unsure`."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(
        A, "run_parakeet",
        lambda d, w, hotwords=None: (
            "pie test tests slash test underscore app dot py hello",
            {"pie": 0.3, "test": 0.3, "hello": 0.3}))

    payload = body(A.transcribe(b"audio bytes", "work", "phone"))
    assert payload["text"] == "pytest tests/test_app.py hello"
    assert "unsure" in payload and payload["unsure"]
    assert "pie" not in payload["unsure"]
    for word in payload["unsure"]:
        assert word.lower().strip(",.!?;:") not in ("pie", "test")


def test_the_engine_parameter_picks_the_engine(installed, parakeet_installed,
                                               sherpa_importable, monkeypatch,
                                               at_a_shell):
    """Both installed, so only the request decides which of them decodes."""
    monkeypatch.setenv("POCKETTUI_WHISPER_BIN", str(installed / "whisper-cli"))
    monkeypatch.setenv("POCKETTUI_WHISPER_MODEL", str(installed / "ggml-base.en.bin"))
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_whisper", lambda b, m, w, p: "git status")
    monkeypatch.setattr(A, "run_parakeet",
                        lambda d, w, hotwords=None: ("git diff", None))

    assert body(A.transcribe(b"audio bytes", "work", "phone",
                             engine="whisper"))["raw"] == "git status"
    assert body(A.transcribe(b"audio bytes", "work", "phone",
                             engine="parakeet"))["raw"] == "git diff"
    # Nothing asked for: the preference still stands.
    assert body(A.transcribe(b"audio bytes", "work", "phone"))["raw"] == "git diff"


def test_asking_for_an_engine_this_install_lacks_is_not_setup(installed,
                                                              monkeypatch):
    """whisper could have answered, but the client is the half that falls back."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    response = A.transcribe(b"audio bytes", "work", "phone", engine="parakeet")
    assert response.status_code == 503
    assert body(response) == {"error": "not_setup"}


def test_an_unknown_engine_parameter_is_ignored(installed, monkeypatch,
                                                at_a_shell):
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_whisper", lambda b, m, w, p: "git status")
    payload = body(A.transcribe(b"audio bytes", "work", "phone", engine="nonesuch"))
    assert payload["raw"] == "git status"


def test_the_env_force_outranks_the_engine_parameter(installed,
                                                     parakeet_installed,
                                                     sherpa_importable,
                                                     monkeypatch, at_a_shell):
    monkeypatch.setenv("POCKETTUI_WHISPER_BIN", str(installed / "whisper-cli"))
    monkeypatch.setenv("POCKETTUI_WHISPER_MODEL", str(installed / "ggml-base.en.bin"))
    monkeypatch.setenv("POCKETTUI_VOICE_ENGINE", "whisper")
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_whisper", lambda b, m, w, p: "git status")

    def explode(*a, **k):
        raise AssertionError("the pinned engine must be the one that runs")

    monkeypatch.setattr(A, "run_parakeet", explode)
    payload = body(A.transcribe(b"audio bytes", "work", "phone", engine="parakeet"))
    assert payload["raw"] == "git status"


def test_parakeet_is_asked_for_hotwords_not_a_prompt(parakeet_installed,
                                                     sherpa_importable,
                                                     monkeypatch, at_a_shell):
    """Parakeet takes no --prompt; the same vocabulary rides hotwords instead."""
    seen = {}
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(R, "history_vocabulary", lambda: ["micromamba"])
    monkeypatch.setattr(R, "ssh_hosts", lambda: [])
    monkeypatch.setattr(R, "dotfile_names", lambda: [])
    monkeypatch.setattr(R, "learned_words", lambda: ["tokenhmr"])

    def spy(model_dir, wav, hotwords=None):
        seen["hotwords"] = hotwords
        return "git status", None

    monkeypatch.setattr(A, "run_parakeet", spy)
    A.transcribe(b"audio bytes", "work", "phone")
    # Learned first, then history — and every line scored, which is the form
    # the per-stream path needs.
    assert seen["hotwords"] == "tokenhmr :0.5\nmicromamba :0.5"


def test_no_vocabulary_means_no_hotwords_argument(parakeet_installed,
                                                  sherpa_importable,
                                                  monkeypatch, at_a_shell):
    """A fresh box has no history and nothing learned: it must not pass ""."""
    seen = {}
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(R, "history_vocabulary", lambda: [])
    monkeypatch.setattr(R, "ssh_hosts", lambda: [])
    monkeypatch.setattr(R, "dotfile_names", lambda: [])
    monkeypatch.setattr(R, "learned_words", lambda: [])

    def spy(model_dir, wav, hotwords=None):
        seen["hotwords"] = hotwords
        return "git status", None

    monkeypatch.setattr(A, "run_parakeet", spy)
    A.transcribe(b"audio bytes", "work", "phone")
    assert seen["hotwords"] is None


def test_a_broken_vocabulary_still_gets_a_transcript(parakeet_installed,
                                                     sherpa_importable,
                                                     monkeypatch, at_a_shell):
    """Biasing is an improvement to the decode, never a precondition for one."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")

    def explode(*a, **k):
        raise RuntimeError("vocabulary store is corrupt")

    monkeypatch.setattr(A, "parakeet_hotwords", explode)
    monkeypatch.setattr(A, "run_parakeet",
                        lambda d, w, hotwords=None: ("git status", None))
    assert body(A.transcribe(b"audio bytes", "work", "phone"))["raw"] == "git status"


def test_hotwords_the_decoder_rejects_fall_back_to_a_plain_decode(
        parakeet_installed, sherpa_importable, monkeypatch, at_a_shell):
    """sherpa-onnx refusing the hotwords costs them, not the user's transcript."""
    calls = []
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(R, "history_vocabulary", lambda: ["micromamba"])
    monkeypatch.setattr(R, "ssh_hosts", lambda: [])
    monkeypatch.setattr(R, "dotfile_names", lambda: [])
    monkeypatch.setattr(R, "learned_words", lambda: [])

    def picky(model_dir, wav, hotwords=None):
        calls.append(hotwords)
        if hotwords:
            raise RuntimeError("hotwords failed to encode")
        return "git status", None

    monkeypatch.setattr(A, "run_parakeet", picky)
    assert body(A.transcribe(b"audio bytes", "work", "phone"))["raw"] == "git status"
    assert calls == ["micromamba :0.5", None]


def test_the_log_line_names_the_engine(parakeet_installed, sherpa_importable,
                                       monkeypatch, at_a_shell):
    """Which model ran, and what it cost, has to be readable from the log."""
    lines = []
    monkeypatch.setattr(A, "log", lines.append)
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_parakeet",
                        lambda d, w, hotwords=None: ("git status", None))
    monkeypatch.setattr(R, "history_vocabulary", lambda: ["micromamba", "sbatch"])
    monkeypatch.setattr(R, "ssh_hosts", lambda: [])
    monkeypatch.setattr(R, "dotfile_names", lambda: [])
    monkeypatch.setattr(R, "learned_words", lambda: [])

    A.transcribe(b"audio bytes", "work", "phone")
    # How much vocabulary the decode was given belongs in the log beside what
    # it cost: a transcript that ignored the user's words and one that never
    # got them read identically otherwise.
    assert any("engine=parakeet" in line and "hotwords=2" in line
               and "decode_ms=" in line for line in lines), lines


def test_the_log_line_names_whisper_too(installed, monkeypatch, at_a_shell):
    lines = []
    monkeypatch.setattr(A, "log", lines.append)
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_whisper", lambda b, m, w, p: "git status")

    A.transcribe(b"audio bytes", "work", "phone")
    assert any("engine=whisper" in line and "decode_ms=" in line
               for line in lines), lines


def test_a_parakeet_crash_answers_a_shape_not_a_traceback(parakeet_installed,
                                                          sherpa_importable,
                                                          monkeypatch, no_tmux):
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")

    def boom(*a, **k):
        raise RuntimeError("onnxruntime exploded")

    monkeypatch.setattr(A, "run_parakeet", boom)
    response = A.transcribe(b"audio bytes", "work", "phone")
    assert response.status_code == 500
    assert body(response) == {"error": "transcribe_failed"}


def parakeet_wav(path):
    """A one-second mono 16-bit wav — what decode_audio would have written."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(array.array("h", [1000] * 16000).tobytes())


@pytest.fixture
def wedged_recognizer(monkeypatch):
    """A recognizer whose decode never returns, as a stuck native call would.

    Returns the event that releases it. The worker thread is left blocked on
    purpose — that is the condition being tested — so the event is set at the
    end to let the thread exit rather than outlive the test.
    """
    import threading
    import types
    release = threading.Event()

    class Stream:
        def accept_waveform(self, rate, samples):
            release.wait(10)

        result = types.SimpleNamespace(text="")

    fake = types.ModuleType("sherpa_onnx")
    fake.OfflineRecognizer = types.SimpleNamespace(
        from_transducer=lambda **kw: types.SimpleNamespace(
            create_stream=lambda hotwords=None: Stream(),
            decode_stream=lambda stream: None))
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    monkeypatch.setattr(A, "PARAKEET_TIMEOUT_S", 0.2)
    yield release
    release.set()


def test_a_wedged_decode_times_out_and_retires_the_engine(parakeet_installed,
                                                          wedged_recognizer,
                                                          tmp_path):
    """A native decode that never returns must not hold the phone past its own
    30 s abort, and the worker it holds cannot be freed: the engine is retired."""
    parakeet_wav(tmp_path / "audio.wav")
    with pytest.raises(subprocess.TimeoutExpired):
        A.run_parakeet(parakeet_installed, tmp_path / "audio.wav")
    assert A._parakeet_dead


def test_the_route_answers_transcribe_timeout_for_a_wedged_decode(
        parakeet_installed, wedged_recognizer, monkeypatch, no_tmux):
    """The same shape whisper's timeout answers with — the phone knows only one."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio",
                        lambda raw, wav, content_type="": parakeet_wav(wav) or "")
    response = A.transcribe(b"audio bytes", "work", "phone")
    assert response.status_code == 500
    assert body(response) == {"error": "transcribe_timeout"}
    assert A._parakeet_dead


def test_a_timed_out_hotword_decode_is_not_retried(parakeet_installed,
                                                   wedged_recognizer,
                                                   monkeypatch, no_tmux):
    """The plain-decode retry is for hotwords the decoder rejected. Taking a
    deadline down that path would queue a second wait behind the stuck worker."""
    calls = []
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio",
                        lambda raw, wav, content_type="": parakeet_wav(wav) or "")
    monkeypatch.setattr(R, "history_vocabulary", lambda: ["micromamba"])
    monkeypatch.setattr(R, "ssh_hosts", lambda: [])
    monkeypatch.setattr(R, "dotfile_names", lambda: [])
    monkeypatch.setattr(R, "learned_words", lambda: [])

    real = A.run_parakeet

    def counted(model_dir, wav, hotwords=None):
        calls.append(hotwords)
        return real(model_dir, wav, hotwords=hotwords)

    monkeypatch.setattr(A, "run_parakeet", counted)
    response = A.transcribe(b"audio bytes", "work", "phone")
    assert body(response) == {"error": "transcribe_timeout"}
    assert calls == ["micromamba :0.5"]


def test_a_retired_engine_falls_back_to_whisper(installed, parakeet_installed,
                                                sherpa_importable, monkeypatch,
                                                at_a_shell):
    """Both installed and Parakeet wedged: the next request still gets a
    transcript, because the retired engine reads as one that is not there."""
    monkeypatch.setenv("POCKETTUI_WHISPER_BIN", str(installed / "whisper-cli"))
    monkeypatch.setenv("POCKETTUI_WHISPER_MODEL", str(installed / "ggml-base.en.bin"))
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_whisper", lambda b, m, w, p: "git status")

    def explode(*a, **k):
        raise AssertionError("a retired engine must not be asked to decode")

    monkeypatch.setattr(A, "run_parakeet", explode)
    monkeypatch.setattr(A, "_parakeet_dead", True)
    assert body(A.transcribe(b"audio bytes", "work", "phone"))["raw"] == "git status"


def test_asking_for_a_retired_engine_is_not_setup(installed, parakeet_installed,
                                                  sherpa_importable, monkeypatch):
    """The client that names an engine owns the fallback, wedged or uninstalled."""
    monkeypatch.setenv("POCKETTUI_WHISPER_BIN", str(installed / "whisper-cli"))
    monkeypatch.setenv("POCKETTUI_WHISPER_MODEL", str(installed / "ggml-base.en.bin"))
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "_parakeet_dead", True)
    response = A.transcribe(b"audio bytes", "work", "phone", engine="parakeet")
    assert response.status_code == 503
    assert body(response) == {"error": "not_setup"}


def test_two_decodes_run_back_to_back_through_the_one_worker(parakeet_installed,
                                                             monkeypatch,
                                                             tmp_path):
    """The single worker is the lock; a decode must release it when it returns."""
    import types

    class Stream:
        def __init__(self):
            self.result = types.SimpleNamespace(text="git status")

        def accept_waveform(self, rate, samples):
            pass

    fake = types.ModuleType("sherpa_onnx")
    fake.OfflineRecognizer = types.SimpleNamespace(
        from_transducer=lambda **kw: types.SimpleNamespace(
            create_stream=lambda hotwords=None: Stream(),
            decode_stream=lambda stream: None))
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)

    parakeet_wav(tmp_path / "audio.wav")
    for _ in range(2):
        assert A.run_parakeet(parakeet_installed,
                              tmp_path / "audio.wav") == ("git status", None)
    assert not A._parakeet_dead


# ---------------------------------------------------------------------------
# Per-word confidence — the decoder's own doubt, read off the pieces
# ---------------------------------------------------------------------------
# The shapes below are the real ones: a live decode of parakeet-tdt-0.6b-v2 in
# modified_beam_search returns BPE pieces where a leading space starts a word,
# punctuation rides the piece before it, and ys_log_probs runs one for one with
# the pieces. `result.words` is empty for this model, so the pieces are all
# there is to work from.

CONFIDENCE_CASES = [
    (
        "a single-piece word is its own log-prob",
        [" how"], [-0.01],
        {"how": pytest.approx(math.exp(-0.01))},
    ),
    (
        "a multi-piece word takes its worst piece, not its average",
        [" camera", "h", "mr"], [-0.02, -1.186, -0.03],
        {"camerahmr": pytest.approx(math.exp(-1.186))},
    ),
    (
        "the leading space is the only word boundary",
        [" git", " stat", "us"], [-0.01, -0.2, -0.05],
        {"git": pytest.approx(math.exp(-0.01)),
         "status": pytest.approx(math.exp(-0.2))},
    ),
    (
        "punctuation attaches to the word before it and is stripped from the key",
        [" how", " are", " you", "?"], [-0.01, -0.02, -0.03, -0.9],
        {"how": pytest.approx(math.exp(-0.01)),
         "are": pytest.approx(math.exp(-0.02)),
         "you": pytest.approx(math.exp(-0.9))},
    ),
    (
        "the first piece starts a word even without a leading space",
        ["git", " diff"], [-0.4, -0.02],
        {"git": pytest.approx(math.exp(-0.4)),
         "diff": pytest.approx(math.exp(-0.02))},
    ),
    (
        "a word said twice keeps the lower confidence",
        [" run", " it", " run"], [-0.01, -0.02, -1.5],
        {"run": pytest.approx(math.exp(-1.5)),
         "it": pytest.approx(math.exp(-0.02))},
    ),
    (
        "the key is lowercased the way the resolver compares tokens",
        [" Token", "HMR", "."], [-0.05, -0.3, -0.01],
        {"tokenhmr": pytest.approx(math.exp(-0.3))},
    ),
    (
        "pieces and log-probs that do not line up are not guessed at",
        [" git", " status"], [-0.01],
        {},
    ),
    ("nothing decoded is nothing to be sure of", [], [], {}),
]


@pytest.mark.parametrize("name,tokens,log_probs,expected", CONFIDENCE_CASES,
                         ids=[c[0] for c in CONFIDENCE_CASES])
def test_word_confidences_are_read_off_the_pieces(name, tokens, log_probs,
                                                  expected):
    assert A._parakeet_word_confidences(tokens, log_probs) == expected


def fake_sherpa(monkeypatch, result):
    """A recognizer whose decode yields `result`, in place of the real one."""
    import types

    class Stream:
        def __init__(self):
            self.result = result

        def accept_waveform(self, rate, samples):
            pass

    fake = types.ModuleType("sherpa_onnx")
    fake.OfflineRecognizer = types.SimpleNamespace(
        from_transducer=lambda **kw: types.SimpleNamespace(
            create_stream=lambda hotwords=None: Stream(),
            decode_stream=lambda stream: None))
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)


def test_a_decode_carries_its_confidences_back(parakeet_installed, monkeypatch,
                                               tmp_path):
    """The pieces are read on the worker that owns the recognizer, and what
    comes back is the transcript plus what the decoder made of each word."""
    import types
    fake_sherpa(monkeypatch, types.SimpleNamespace(
        text="run camerahmr",
        tokens=[" run", " camera", "h", "mr"],
        ys_log_probs=[-0.01, -0.02, -1.186, -0.03]))

    parakeet_wav(tmp_path / "audio.wav")
    text, confidence = A.run_parakeet(parakeet_installed, tmp_path / "audio.wav")
    assert text == "run camerahmr"
    assert confidence == {"run": pytest.approx(math.exp(-0.01)),
                          "camerahmr": pytest.approx(math.exp(-1.186))}


def test_a_result_without_log_probs_still_transcribes(parakeet_installed,
                                                      monkeypatch, tmp_path):
    """Confidence is an improvement on the transcript, never a precondition:
    a result that carries no per-piece scores costs the confidences, not the
    dictation."""
    import types
    fake_sherpa(monkeypatch, types.SimpleNamespace(text="git status",
                                                   tokens=[" git", " status"]))

    parakeet_wav(tmp_path / "audio.wav")
    assert A.run_parakeet(parakeet_installed,
                          tmp_path / "audio.wav") == ("git status", None)


def test_the_confidences_stay_out_of_the_response(parakeet_installed,
                                                  sherpa_importable,
                                                  monkeypatch, at_a_shell):
    """The raw per-word probabilities are a decode detail, never handed over
    whole — only a surviving low-confidence word's surface form, via `unsure`."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_parakeet",
                        lambda d, w, hotwords=None: ("git status", {"git": 0.3}))
    payload = body(A.transcribe(b"audio bytes", "work", "phone"))
    assert set(payload) == {"text", "raw", "ms", "unsure"}


def test_the_log_line_names_the_doubted_words(parakeet_installed,
                                              sherpa_importable, monkeypatch,
                                              at_a_shell):
    """A transcript that came out wrong is explained by the words the decoder
    was least sure of, so those are what the line carries."""
    lines = []
    monkeypatch.setattr(A, "log", lines.append)
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_parakeet",
                        lambda d, w, hotwords=None: ("run camerahmr", {
                            "run": 0.99, "camerahmr": 0.31}))
    A.transcribe(b"audio bytes", "work", "phone")
    assert any("doubted=camerahmr:0.31,run:0.99" in line
               for line in lines), lines


def test_the_whisper_path_has_no_confidences(installed, monkeypatch, at_a_shell):
    """whisper offers none, and the line says so rather than going missing."""
    lines = []
    monkeypatch.setattr(A, "log", lines.append)
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_whisper", lambda b, m, w, p: "git status")
    A.transcribe(b"audio bytes", "work", "phone")
    assert any("doubted=-" in line for line in lines), lines


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


def test_a_clip_that_hit_the_decode_cap_is_flagged_truncated(installed, monkeypatch,
                                                              at_a_shell):
    """ffmpeg's `-t MAX_AUDIO_SECONDS` silently drops anything past the cap —
    the phone can only know its recording was cut short if the reply says so.
    """
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "is_silent",
                        lambda wav: A.SilenceCheck(False, duration_s=A.MAX_AUDIO_SECONDS))
    monkeypatch.setattr(A, "run_whisper", lambda b, m, w, p: "git status")
    response = A.transcribe(b"audio bytes", "work", "phone")
    assert body(response)["truncated"] is True


def test_a_short_clip_is_not_flagged_truncated(installed, monkeypatch, at_a_shell):
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "is_silent",
                        lambda wav: A.SilenceCheck(False, duration_s=3.0))
    monkeypatch.setattr(A, "run_whisper", lambda b, m, w, p: "git status")
    response = A.transcribe(b"audio bytes", "work", "phone")
    assert "truncated" not in body(response)


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
    vocab = prompt.split(": ", 1)[1].rstrip(".").split(" ")
    assert len(vocab) == len(set(v.lower() for v in vocab))


def test_prompt_vocabulary_is_space_separated_not_comma_joined(no_tmux):
    """A comma-separated prompt teaches whisper to emit comma-fragmented
    transcripts (whisper conditions on the prompt as recently-decoded text);
    space-separating keeps the vocabulary bias without the format imitation.
    """
    prompt = A.transcribe_prompt(["$ pytest tests/test_camerahmr.py"], "")
    assert ", " not in prompt.split(": ", 1)[1]


def test_prompt_includes_the_login_username(no_tmux, monkeypatch):
    monkeypatch.setattr(A.getpass, "getuser", lambda: "sdwivedi")
    prompt = A.transcribe_prompt([], "")
    assert "sdwivedi" in prompt


def test_prompt_is_empty_without_any_vocabulary(no_tmux, monkeypatch):
    """Nothing worth saying means no --prompt at all, not an empty sentence."""
    monkeypatch.setattr(A.getpass, "getuser", lambda: (_ for _ in ()).throw(Exception()))
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
    # The prompt biases the decode, so leaving these on their real sources would
    # feed the model whatever this machine's shell history holds that minute —
    # the transcript then changes between runs of the same test.
    monkeypatch.setattr(R, "history_vocabulary", lambda deadline=0.0: [])
    monkeypatch.setattr(R, "ssh_hosts", lambda: [])
    monkeypatch.setattr(R, "dotfile_names", lambda: [])
    # The corrections store, not learned_words(): it is what both the prompt and
    # the ASR rules read, so pinning it settles the whole learned channel.
    monkeypatch.setattr(R, "learned_corrections", lambda path=None: [])

    response = A.transcribe((BENCH_AUDIO / "p01.wav").read_bytes(), "work", "phone")
    assert response.status_code == 200
    payload = body(response)

    assert payload["raw"], "the real binary produced no transcript at all"
    # The repair the pipeline exists to make: the model's "test/" becomes the
    # directory that is actually there, with the filename spelled correctly.
    assert "tests/test_camerahmr.py" in payload["text"]
    assert payload["ms"] > 0


def _real_parakeet_reason() -> str:
    """Why the real-Parakeet test cannot run here, or "" if it can.

    Evaluated at collection, and deliberately not via parakeet_available(): the
    autouse fixture blanks PARAKEET_DIR for every other test, and this needs the
    unpatched answer.
    """
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:  # noqa: BLE001
        return "sherpa-onnx is not installed"
    if A.parakeet_model_dir() is None:
        return "voice/parakeet/ holds no model (run setup_voice.sh)"
    if not (BENCH_AUDIO / "p01.wav").exists():
        return "benchmark audio is not present"
    return ""


@pytest.mark.skipif(bool(_real_parakeet_reason()), reason=_real_parakeet_reason())
def test_real_audio_through_parakeet(tmp_path, monkeypatch):
    """The same clip, end to end through the real ONNX model.

    Parakeet gets the command word right where base.en does not — it writes
    "run pytest on ...", not "Run pitest on ..." — and it reassembles the spoken
    filename into `test_camerahmr.py`, which is the repair this pipeline exists
    to make.

    The directory is deliberately NOT asserted, and the difference is worth
    recording. Parakeet transcribes verbatim: it writes the spoken separator as
    the word "slash", where whisper writes the character. The resolver's path
    snapper matches candidates that already contain a "/", so whisper's
    "test/test_camerahmr.py" reaches it and becomes "tests/test_camerahmr.py"
    while Parakeet's "test slash test_camerahmr.py" does not — a spoken "slash"
    between two words is never turned into a separator. That is a pre-existing
    gap in the resolver, not in this engine, and closing it is a resolver change
    with its own benchmark to answer to; the test documents the current
    behaviour rather than asserting a repair the pipeline does not yet make.
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_camerahmr.py").write_text("")
    (tmp_path / "app.py").write_text("")
    R._cwd_cache.clear()

    # Undo the autouse blanking: this is the one test that wants the real model.
    monkeypatch.setattr(A, "PARAKEET_DIR", A.VOICE_DIR / "parakeet")
    monkeypatch.setenv("POCKETTUI_VOICE_ENGINE", "parakeet")
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

    assert payload["raw"], "the real model produced no transcript at all"
    # The filename, spelled the way the filesystem spells it — which the model
    # heard as "test underscore camera hmr dot py" and could not have known.
    assert "test_camerahmr.py" in payload["text"]
    assert "camera hmr" not in payload["text"]
    assert payload["ms"] > 0


# ---------------------------------------------------------------------------
# Rarity-ranked prompt budget
# ---------------------------------------------------------------------------

def vocab_of(prompt: str) -> list[str]:
    """The words of a prompt, without the lead sentence or the final period."""
    return prompt.split(": ", 1)[1].rstrip(".").split(" ")


def test_prompt_excludes_bare_common_words(no_tmux):
    """`report` and `machine` are words whisper already spells; they bias
    nothing and would spend budget a real identifier needs."""
    prompt = A.transcribe_prompt(
        ["the report machine data results", "test_camerahmr.py"], "")
    words = [w.lower() for w in vocab_of(prompt)]
    assert "test_camerahmr.py" in words
    for common in ("report", "machine", "data", "results"):
        assert common not in words, common


def test_prompt_ranks_a_rare_identifier_above_a_dictionary_word(no_tmux):
    """Budget is spent on rarity, not on the order the sources produced.

    `test_camerahmr.py` is unreachable for the model unaided even though it
    appears last; `pancake` is a plain lowercase word it writes on its own.
    """
    prompt = A.transcribe_prompt(["pancake wagon test_camerahmr.py"], "")
    words = [w.lower() for w in vocab_of(prompt)]
    assert words.index("test_camerahmr.py") < words.index("pancake")


def test_prompt_excludes_an_inflected_common_word(no_tmux):
    """The list holds base forms; "results" is no rarer than "result"."""
    prompt = A.transcribe_prompt(["results running machines reported"], "")
    words = [w.lower() for w in vocab_of(prompt)]
    for common in ("results", "running", "machines", "reported"):
        assert common not in words, common


def test_prompt_keeps_rare_words_when_the_budget_is_tight(no_tmux):
    """The cap cuts from the bottom of the ranking, not the end of the list.

    A screenful of forgettable lowercase words ahead of one separator-carrying
    identifier used to push that identifier out of a full prompt entirely.
    """
    screen = [" ".join(f"widget{n}" for n in range(400))]
    screen.append("tests/test_camerahmr.py")
    prompt = A.transcribe_prompt(screen, "")
    assert len(prompt) <= A.MAX_PROMPT_CHARS
    assert "tests/test_camerahmr.py" in prompt


def test_prompt_includes_history_vocabulary(no_tmux):
    """The motivating failure: a path in neither the screen nor the cwd."""
    prompt = A.transcribe_prompt(["$ ls"], "", ["sdwivedi", "cluster", "lustre"])
    for word in ("sdwivedi", "cluster", "lustre"):
        assert word in prompt, word


def test_prompt_keeps_the_username_even_against_a_full_budget(no_tmux, monkeypatch):
    """The one always-included word: home paths are dictated constantly."""
    monkeypatch.setattr(A.getpass, "getuser", lambda: "sdwivedi")
    screen = [" ".join(f"ident_number_{n}" for n in range(500))]
    prompt = A.transcribe_prompt(screen, "")
    assert len(prompt) <= A.MAX_PROMPT_CHARS
    assert "sdwivedi" in vocab_of(prompt)


def test_prompt_keeps_the_username_when_another_source_also_offers_it(
        no_tmux, monkeypatch):
    """The username appearing in a second source must not cost it its slot.

    `seen` records that a word was scored, not that it will survive the cut, so
    reserving the username only when it was `not in seen` dropped it whenever
    another source had also offered it — here history, whose entries score far
    below a cwd listing large enough to fill every slot on its own.
    """
    monkeypatch.setattr(A.getpass, "getuser", lambda: "crowded_user")
    names = [f"deep_module_{n}/leaf_{n}.py" for n in range(40)]
    monkeypatch.setattr(R, "cwd_vocabulary", lambda cwd: (names, []))
    prompt = A.transcribe_prompt([], "/some/where", ["crowded_user", "cluster"])
    assert "crowded_user" in vocab_of(prompt)
    assert len(vocab_of(prompt)) <= A.MAX_PROMPT_WORDS
    assert len(prompt) <= A.MAX_PROMPT_CHARS
    # Reserved, not duplicated: `seen` still keeps the later sources off it.
    assert vocab_of(prompt).count("crowded_user") == 1


def test_prompt_ordering_is_deterministic(no_tmux):
    screen = ["pytest tests/test_camerahmr.py resolver.py CameraHMR run2"]
    history = ["sdwivedi", "cluster", "micromamba"]
    first = A.transcribe_prompt(screen, "", history)
    for _ in range(5):
        assert A.transcribe_prompt(screen, "", history) == first


def test_prompt_prefers_the_screen_over_history_at_equal_rarity(no_tmux):
    """Both are rare; the word the user is looking at ranks first."""
    prompt = A.transcribe_prompt(["screenhmr_thing.py"], "",
                                 ["historyhmr_thing.py"])
    words = vocab_of(prompt)
    assert words.index("screenhmr_thing.py") < words.index("historyhmr_thing.py")


def test_prompt_history_degrades_to_the_old_behaviour_when_absent(no_tmux):
    """History is optional everywhere: a box with no history file is normal."""
    assert A.transcribe_prompt(["test_camerahmr.py"], "", None) == \
        A.transcribe_prompt(["test_camerahmr.py"], "")


def test_prompt_never_carries_a_credential_from_history(no_tmux, tmp_path,
                                                        monkeypatch):
    """End to end through the real history reader, not a hand-made word list."""
    hist = tmp_path / "history"
    hist.write_text(
        ": 1700000000:0;export HF_TOKEN=hf_QQzzXXsecretvalue1234567\n"
        ": 1700000001:0;cd /is/cluster/fast/sdwivedi/work\n")
    monkeypatch.setenv("HISTFILE", str(hist))
    R._history_cache["stamp"] = None
    R._history_cache["words"] = []
    try:
        prompt = A.transcribe_prompt([], "", R.history_vocabulary())
        assert "sdwivedi" in prompt
        assert "secretvalue" not in prompt
    finally:
        R._history_cache["stamp"] = None
        R._history_cache["words"] = []


def test_prompt_is_capped_by_word_count_not_only_characters(no_tmux):
    """Past roughly sixty words whisper imitates the prompt's format — it runs
    spoken words together ("run pytest on tests" -> "runPitastonTest") instead
    of merely taking the vocabulary. Rarity ranking fills the prompt with long
    paths, so the character budget alone no longer bounds the word count."""
    screen = [" ".join(f"ident_number_{n}" for n in range(500))]
    prompt = A.transcribe_prompt(screen, "", ["sdwivedi", "cluster"])
    assert len(vocab_of(prompt)) <= A.MAX_PROMPT_WORDS
    assert len(prompt) <= A.MAX_PROMPT_CHARS


def test_prompt_fill_skips_an_oversize_word_rather_than_stopping(no_tmux):
    """One path too long for the remaining room must not end the fill while
    shorter words behind it still fit."""
    long_path = "a_very/" * 30 + "leaf.py"
    prompt = A.transcribe_prompt([long_path, "short_one.py"], "")
    assert "short_one.py" in vocab_of(prompt)


# ---------------------------------------------------------------------------
# Scrollback, for the prompt only
# ---------------------------------------------------------------------------

@pytest.fixture
def recording_capture(monkeypatch, no_tmux):
    """A pane whose capture_pane calls are recorded, visible vs scrollback.

    The wide capture returns the visible screen preceded by older lines, which
    is what a real `capture-pane -S -200` gives back: the tail of a longer
    buffer, newest last.
    """
    calls: list[int] = []
    scrolled_off = ["sai@box:~$ vim older_scrollback_file.py"]

    def capture(name, lines=60):
        calls.append(lines)
        return SHELL_SCREEN if lines <= 60 else scrolled_off + SHELL_SCREEN

    monkeypatch.setattr(A, "resolve_target", lambda s, d: "work")
    monkeypatch.setattr(A, "capture_pane", capture)
    monkeypatch.setattr(A, "pane_cwd", lambda name: "")
    return calls


def test_the_prompt_gets_its_own_wider_capture(installed, monkeypatch,
                                               recording_capture):
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_whisper", lambda b, m, w, p: "git status")

    A.transcribe(b"audio bytes", "work", "phone")
    assert 60 in recording_capture, "the visible capture must keep its default"
    assert A.PROMPT_SCROLLBACK_LINES in recording_capture


def test_the_resolver_only_ever_sees_the_visible_screen(installed, monkeypatch,
                                                        recording_capture):
    """Register detection and window matching are entitled to exactly the pane
    the user is looking at; the wider capture is prompt vocabulary alone."""
    seen: dict = {}
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A, "run_whisper", lambda b, m, w, p: "git status")

    real_resolve = R.resolve

    def spy(text, **kwargs):
        seen.update(kwargs)
        return real_resolve(text, **kwargs)

    monkeypatch.setattr(A.resolver, "resolve", spy)
    A.transcribe(b"audio bytes", "work", "phone")
    assert seen["screen"] == SHELL_SCREEN


def test_the_prompt_reaches_words_that_scrolled_off(no_tmux):
    """The point of the wider capture: MAX_SCREEN_LINES would have cut these."""
    # Ordinary English filler, which the rarity gate drops: the assertion is
    # about reach past the 60-line window, not about the word budget.
    scrollback = ["the report was ready" for _ in range(150)]
    scrollback.insert(0, "vim scrolled_off_thing.py")
    prompt = A.transcribe_prompt(SHELL_SCREEN, "", None, scrollback)
    assert "scrolled_off_thing.py" in prompt


def test_the_visible_screen_still_outranks_the_scrollback(no_tmux):
    """Both are rare; what is on screen right now ranks first."""
    prompt = A.transcribe_prompt(["visiblehmr_thing.py"], "", None,
                                 ["scrolledhmr_thing.py", "visiblehmr_thing.py"])
    words = vocab_of(prompt)
    assert words.index("visiblehmr_thing.py") < words.index("scrolledhmr_thing.py")


def test_the_prompt_degrades_to_the_old_behaviour_without_scrollback(no_tmux):
    assert A.transcribe_prompt(["test_camerahmr.py"], "", None, None) == \
        A.transcribe_prompt(["test_camerahmr.py"], "")


# ---------------------------------------------------------------------------
# SSH hosts in the prompt vocabulary
# ---------------------------------------------------------------------------

def test_ssh_hosts_ride_the_history_channel(installed, monkeypatch, at_a_shell):
    """One combined list, so they reach both the prompt and the resolver."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A.resolver, "history_vocabulary", lambda: ["sdwivedi"])
    monkeypatch.setattr(A.resolver, "ssh_hosts", lambda: ["galtonhost"])
    monkeypatch.setattr(A.resolver, "dotfile_names", lambda: [])

    prompts: list[str] = []
    monkeypatch.setattr(A, "run_whisper",
                        lambda b, m, w, p: prompts.append(p) or "git status")
    seen: dict = {}
    real_resolve = R.resolve

    def spy(text, **kwargs):
        seen.update(kwargs)
        return real_resolve(text, **kwargs)

    monkeypatch.setattr(A.resolver, "resolve", spy)
    A.transcribe(b"audio bytes", "work", "phone")

    assert "galtonhost" in prompts[0]
    # After history in the combined list: same channel, at the tail.
    assert seen["extra_vocab"] == ["sdwivedi", "galtonhost"]


# ---------------------------------------------------------------------------
# Home dotfiles in the request vocabulary
# ---------------------------------------------------------------------------

def test_dotfile_names_ride_the_history_channel(installed, monkeypatch,
                                                at_a_shell):
    """"open bashrc" has no other source: a zsh user's history never held it."""
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(A.resolver, "history_vocabulary", lambda: ["sdwivedi"])
    monkeypatch.setattr(A.resolver, "ssh_hosts", lambda: ["galtonhost"])
    monkeypatch.setattr(A.resolver, "dotfile_names", lambda: ["bashrc", "zshrc"])

    prompts: list[str] = []
    monkeypatch.setattr(A, "run_whisper",
                        lambda b, m, w, p: prompts.append(p) or "git status")
    seen: dict = {}
    real_resolve = R.resolve

    def spy(text, **kwargs):
        seen.update(kwargs)
        return real_resolve(text, **kwargs)

    monkeypatch.setattr(A.resolver, "resolve", spy)
    A.transcribe(b"audio bytes", "work", "phone")

    assert "bashrc" in prompts[0]
    # Last in the combined list: the same low-weight channel, at its tail.
    assert seen["extra_vocab"] == ["sdwivedi", "galtonhost", "bashrc", "zshrc"]


def test_dotfile_names_reach_the_parakeet_hotwords(parakeet_installed,
                                                   sherpa_importable,
                                                   monkeypatch, at_a_shell):
    """The engine that missed this word takes vocabulary by hotwords, not prompt."""
    seen = {}
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(R, "history_vocabulary", lambda: [])
    monkeypatch.setattr(R, "ssh_hosts", lambda: [])
    monkeypatch.setattr(R, "dotfile_names", lambda: ["bashrc", "vimrc"])
    monkeypatch.setattr(R, "learned_words", lambda: [])

    def spy(model_dir, wav, hotwords=None):
        seen["hotwords"] = hotwords
        return "git status", None

    monkeypatch.setattr(A, "run_parakeet", spy)
    A.transcribe(b"audio bytes", "work", "phone")
    assert seen["hotwords"] == "bashrc :0.5\nvimrc :0.5"


def test_the_word_confidences_reach_the_resolver(parakeet_installed,
                                                 sherpa_importable,
                                                 monkeypatch, at_a_shell):
    """What the engine reports about each word has to survive the trip.

    The resolver decides what the numbers mean; this route only has to hand
    them over, and hand over None when the engine had nothing to say.
    """
    seen = {}
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(A, "decode_audio", lambda raw, wav, content_type="": "")
    monkeypatch.setattr(R, "history_vocabulary", lambda: [])
    monkeypatch.setattr(R, "ssh_hosts", lambda: [])
    monkeypatch.setattr(R, "dotfile_names", lambda: [])
    monkeypatch.setattr(R, "learned_words", lambda: [])

    def spy(text, **kwargs):
        seen.update(kwargs)
        return {"text": text, "register": "shell", "spans": []}

    monkeypatch.setattr(A.resolver, "resolve", spy)

    heard = {"git": 0.99, "status": 0.42}
    monkeypatch.setattr(A, "run_parakeet",
                        lambda model_dir, wav, hotwords=None: ("git status", heard))
    A.transcribe(b"audio bytes", "work", "phone")
    assert seen["confidence"] == heard

    # The whisper path reports nothing, and must say so rather than inventing
    # an empty dict the resolver would have to tell apart from a real one.
    seen.clear()
    monkeypatch.setattr(A, "run_parakeet",
                        lambda model_dir, wav, hotwords=None: ("git status", None))
    A.transcribe(b"audio bytes", "work", "phone")
    assert seen["confidence"] is None


def test_learned_words_still_outrank_the_dotfiles(monkeypatch):
    """The new source joins the low-priority tail; it does not jump the queue."""
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    text = A.parakeet_hotwords(history=["micromamba", "bashrc"],
                               learned=["tokenhmr"])
    assert lines_of(text) == ["tokenhmr", "micromamba", "bashrc"]


def test_a_dotfile_named_after_an_english_word_is_dropped(monkeypatch):
    """A dotfile whose name is a word Parakeet already writes buys no slot.

    The history filters run over this source unchanged, which is the point of
    routing it through the same channel rather than a new one.
    """
    monkeypatch.delenv("POCKETTUI_HOTWORD_SCORE", raising=False)
    text = A.parakeet_hotwords(history=["local", "bashrc"])
    assert lines_of(text) == ["bashrc"]


# ---------------------------------------------------------------------------
# Learned corrections
# ---------------------------------------------------------------------------


def test_learned_words_are_capped_in_the_prompt(no_tmux, monkeypatch):
    """A growing store must not squeeze out the screen, the cwd or the name.

    Learned words carry the top source weight, so without the cap a user who
    has taught the app twenty words would have a prompt of nothing else.
    """
    monkeypatch.setattr(A.getpass, "getuser", lambda: "sdwivedi")
    learned = [f"learnedword{i}" for i in range(20)]
    prompt = A.transcribe_prompt(["$ pytest tests/test_camerahmr.py"], "",
                                 learned=learned)
    present = [w for w in learned if w in prompt]
    assert len(present) == A.MAX_PROMPT_LEARNED
    # Most recently reinforced first: the head of the list is what is kept.
    assert present == learned[:A.MAX_PROMPT_LEARNED]
    # And the reservations that were there before still hold.
    assert "sdwivedi" in prompt
    assert "test_camerahmr.py" in prompt


def test_a_plain_looking_learned_word_survives_the_shape_test(no_tmux, monkeypatch):
    """The shape test is a guess about need; a learned word carries the answer.

    `report` is a common English word and is dropped outright from every other
    source. Learned, it is a word whisper demonstrably got wrong for this user
    — the shape test simply has no way to know that, and the store does.
    """
    monkeypatch.setattr(A.getpass, "getuser", lambda: "sdwivedi")
    assert A._prompt_rarity("report") == 0.0
    assert "report" not in A.transcribe_prompt(["report"], "")
    assert "report" in A.transcribe_prompt([], "", learned=["report"])


def test_learned_words_default_to_none(no_tmux, monkeypatch):
    """The parameter is optional: every existing caller keeps working."""
    monkeypatch.setattr(A.getpass, "getuser", lambda: "sdwivedi")
    assert A.transcribe_prompt(["$ pytest"], "") == \
        A.transcribe_prompt(["$ pytest"], "", learned=[])


def test_learn_records_a_repeated_mishearing(tmp_path, monkeypatch):
    """The route carries the pair across; the resolver decides what it means."""
    store = tmp_path / "learned.json"
    monkeypatch.setattr(R, "LEARNED_PATH", store)
    monkeypatch.setattr(R, "_learned_cache", {"stamp": None, "entries": []})

    assert body(A.api_learn({"heard": "cd stved", "sent": "cd sdwivedi"})) == \
        {"learned": 1}
    assert R.learned_words(store) == []          # one event is not evidence
    A.api_learn({"heard": "open stved", "sent": "open sdwivedi"})
    assert R.learned_words(store) == ["sdwivedi"]


def test_learn_ignores_an_unedited_or_empty_send(tmp_path, monkeypatch):
    """Nothing to compare, nothing to learn — and never an error."""
    store = tmp_path / "learned.json"
    monkeypatch.setattr(R, "LEARNED_PATH", store)
    monkeypatch.setattr(R, "_learned_cache", {"stamp": None, "entries": []})

    for payload in ({"heard": "cd stved", "sent": "cd stved"},
                    {"heard": "", "sent": "cd sdwivedi"},
                    {"heard": "cd stved", "sent": ""},
                    {}):
        assert body(A.api_learn(payload)) == {"learned": 0}
    assert not store.exists()


def test_learn_never_fails_the_send(tmp_path, monkeypatch):
    """The phone fires this and forgets it; there is no failure for it to see."""
    monkeypatch.setattr(R, "LEARNED_PATH", tmp_path / "nope" / "learned.json")
    monkeypatch.setattr(R, "_learned_cache", {"stamp": None, "entries": []})
    response = A.api_learn({"heard": "cd stved", "sent": "cd sdwivedi"})
    assert response.status_code == 200
    assert body(response) == {"learned": 0}


def test_an_unreadable_store_leaves_the_prompt_alone(tmp_path, monkeypatch):
    """A corrupt store is an empty vocabulary everywhere, including here."""
    store = tmp_path / "learned.json"
    store.write_text("{ not json")
    monkeypatch.setattr(R, "LEARNED_PATH", store)
    monkeypatch.setattr(R, "_learned_cache", {"stamp": None, "entries": []})
    assert R.learned_words() == []
    monkeypatch.setattr(A.getpass, "getuser", lambda: "sdwivedi")
    assert A.transcribe_prompt([], "", learned=R.learned_words()) == \
        A.transcribe_prompt([], "")


def test_api_learned_lists_newest_use_first(tmp_path, monkeypatch):
    """Every entry is exposed, promoted or not, ordered by when it last fired."""
    store = tmp_path / "learned.json"
    monkeypatch.setattr(R, "LEARNED_PATH", store)
    monkeypatch.setattr(R, "_learned_cache", {"stamp": None, "entries": []})

    R.learn_corrections("cd stved", "cd sdwivedi", now=1.0)
    R.learn_corrections("open stved", "open sdwivedi", now=2.0)   # promoted
    R.learn_corrections("run camerahmr", "run tokenhmr", now=3.0)  # unpromoted

    entries = body(A.api_learned())["entries"]
    assert [(e["wrong"], e["right"]) for e in entries] == \
        [("camerahmr", "tokenhmr"), ("stved", "sdwivedi")]
    assert entries[0]["promoted"] is False
    assert entries[0]["count"] == 1
    assert entries[0]["utterances"] == 1
    assert entries[0]["last_ts"] == 3.0
    assert entries[1]["promoted"] is True
    assert entries[1]["count"] == 2
    assert entries[1]["utterances"] == 2


def test_api_learned_empty_store(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "LEARNED_PATH", tmp_path / "learned.json")
    monkeypatch.setattr(R, "_learned_cache", {"stamp": None, "entries": []})
    assert body(A.api_learned()) == {"entries": []}


def test_api_learned_delete_removes_exactly_one_entry(tmp_path, monkeypatch):
    """Deleting a pair also stops it firing on the very next resolve."""
    store = tmp_path / "learned.json"
    monkeypatch.setattr(R, "LEARNED_PATH", store)
    monkeypatch.setattr(R, "_learned_cache", {"stamp": None, "entries": []})

    R.learn_corrections("cd stved", "cd sdwivedi", now=1.0)
    R.learn_corrections("open stved", "open sdwivedi", now=2.0)
    R.learn_corrections("run camerahmr", "run tokenhmr", now=3.0)

    assert body(A.api_learned_delete({"wrong": "stved", "right": "sdwivedi"})) == \
        {"deleted": 1}
    remaining = {(e["wrong"], e["right"]) for e in body(A.api_learned())["entries"]}
    assert remaining == {("camerahmr", "tokenhmr")}
    assert R.apply_learned_rules("cd stved") == "cd stved"


def test_api_learned_delete_is_case_insensitive(tmp_path, monkeypatch):
    store = tmp_path / "learned.json"
    monkeypatch.setattr(R, "LEARNED_PATH", store)
    monkeypatch.setattr(R, "_learned_cache", {"stamp": None, "entries": []})
    R.learn_corrections("cd stved", "cd sdwivedi", now=1.0)
    R.learn_corrections("open stved", "open sdwivedi", now=2.0)
    assert body(A.api_learned_delete({"wrong": "STVED", "right": "Sdwivedi"})) == \
        {"deleted": 1}
    assert body(A.api_learned()) == {"entries": []}


def test_api_learned_delete_nonexistent_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "LEARNED_PATH", tmp_path / "learned.json")
    monkeypatch.setattr(R, "_learned_cache", {"stamp": None, "entries": []})
    assert body(A.api_learned_delete({"wrong": "nope", "right": "nada"})) == \
        {"deleted": 0}


def test_api_learned_delete_all(tmp_path, monkeypatch):
    store = tmp_path / "learned.json"
    monkeypatch.setattr(R, "LEARNED_PATH", store)
    monkeypatch.setattr(R, "_learned_cache", {"stamp": None, "entries": []})
    R.learn_corrections("cd stved", "cd sdwivedi", now=1.0)
    R.learn_corrections("open stved", "open sdwivedi", now=2.0)
    R.learn_corrections("run camerahmr", "run tokenhmr", now=3.0)

    assert body(A.api_learned_delete({"all": True})) == {"deleted": 2}
    assert body(A.api_learned()) == {"entries": []}
    assert body(A.api_learned_delete({"all": True})) == {"deleted": 0}


# ---------------------------------------------------------------------------
# Build version
# ---------------------------------------------------------------------------

def test_version_is_read_from_the_stamped_file(tmp_path, monkeypatch):
    """The trailing newline git archive leaves behind is not part of the value."""
    stamp = tmp_path / "VERSION"
    stamp.write_text("0.1.123\n")
    monkeypatch.setattr(A, "VERSION_PATH", stamp)
    assert body(A.api_version())["version"] == "0.1.123"


def test_an_unstamped_install_reports_an_empty_version(tmp_path, monkeypatch):
    """A checkout and a pre-versioning install both answer, they just say nothing."""
    monkeypatch.setattr(A, "VERSION_PATH", tmp_path / "absent" / "VERSION")
    response = A.api_version()
    assert response.status_code == 200
    assert body(response)["version"] == ""


def test_version_carries_a_capability_map():
    """What the shell reads to stop offering what an older server cannot serve.

    Only the shape and the one flag with a fixed answer are pinned: the rest of
    the map is the feature list, and a test restating it would only ever be
    updated to match. "update" is not fixed — it depends on whether the machine
    running the suite has a `pockettui` wrapper installed — so it is checked
    against the same question the map asks (see tests/test_update.py).
    """
    caps = body(A.api_version())["capabilities"]
    assert isinstance(caps, dict) and caps
    assert all(isinstance(v, bool) for v in caps.values()), caps
    assert caps["fs"] is True
    assert caps["update"] == (A.update_command() is not None)
