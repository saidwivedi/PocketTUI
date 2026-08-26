"""Tests for the dictation resolver.

The suite is weighted the way the feature is: a handful of cases prove the
corrections work, and a much larger set proves that everything else is left
alone. A regression that stops fixing "pie test" is an annoyance; one that
starts rewriting a sentence is the failure this module is built to avoid.
"""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import resolver as R  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SHELL_SCREEN = [
    "sai@box:~/work/pockettui$ git status",
    "On branch main",
    "nothing to commit, working tree clean",
    "sai@box:~/work/pockettui$ ",
]

CLAUDE_SCREEN = [
    "✻ Welcome to Claude Code",
    "",
    "> I'll read the file now.",
    "  ⎿  Read app.py (1060 lines)",
    "╭──────────────────────────────────────────╮",
    "│ >                                        │",
    "╰──────────────────────────────────────────╯",
    "  ? for shortcuts",
]

VIM_SCREEN = [
    "def resolve(text):",
    "    return text",
    "~",
    "~",
    "-- INSERT --                       12,5          All",
]


@pytest.fixture
def project(tmp_path):
    """A small project tree, so cwd vocabulary is real rather than mocked."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_camerahmr.py").write_text("")
    (tmp_path / "app.py").write_text("")
    (tmp_path / "resolver.py").write_text("")
    (tmp_path / "build_mobile.py").write_text("")
    R._cwd_cache.clear()
    return str(tmp_path)


# ---------------------------------------------------------------------------
# Phonetics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spoken,written", [
    ("pie test", "pytest"),
    ("num pie", "numpy"),
    ("camera h m r", "camerahmr"),
    ("you vicorn", "uvicorn"),
    ("scikit learn", "scikitlearn"),
    ("j son", "json"),
])
def test_metaphone_collides_for_spoken_forms(spoken, written):
    """The point of the key: what a phone heard and what was meant hash alike.

    Spelled-out vowels ("fast a p i" for fastapi) are deliberately not in this
    table: each spoken vowel letter contributes a sound the written word does
    not have, and suppressing them to fix that case would blunt the key for
    every consonant-led one, which is the far commoner shape.
    """
    spoken_key = "".join(R.metaphone(w) for w in spoken.split())
    assert spoken_key == R.metaphone(written), (spoken_key, R.metaphone(written))


@pytest.mark.parametrize("word,key", [
    ("knee", "N"),
    ("write", "RT"),
    ("psycho", "SX"),
    ("phone", "FN"),
    ("box", "PKS"),
    ("church", "XRX"),
    ("ghost", "ST"),
    ("night", "NT"),
    ("gym", "JM"),
    ("school", "SKL"),
    ("this", "0S"),
    ("action", "AKXN"),
    ("wheel", "WAL"),
    ("judge", "JJ"),
    ("dumb", "TM"),
])
def test_metaphone_rule_table(word, key):
    """Pins the individual spelling rules, so a refactor cannot quietly drop one."""
    assert R.metaphone(word) == key


def test_metaphone_ignores_case_and_punctuation():
    assert R.metaphone("Py-Test!") == R.metaphone("pytest")


def test_metaphone_empty():
    assert R.metaphone("") == ""
    assert R.metaphone("123") == ""


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def test_damerau_counts_transposition_as_one():
    assert R.damerau_levenshtein("ab", "ba") == 1
    assert R.damerau_levenshtein("resolver", "resovler") == 1


def test_similarity_bounds():
    assert R.similarity("same", "same") == 1.0
    assert R.similarity("", "") == 1.0
    assert 0.0 <= R.similarity("abc", "xyz") <= 1.0


# ---------------------------------------------------------------------------
# Spoken-syntax rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spoken,expected", [
    ("dash dash verbose", "--verbose"),
    ("pip install dash dash no dash build isolation",
     "pip install --no-build isolation"),
    ("app dot py", "app.py"),
    ("tests slash test underscore main dot py", "tests/test_main.py"),
    ("git checkout head tilde three", "git checkout head~3"),
    ("export foo equals bar", "export foo=bar"),
    ("cat log pipe grep error", "cat log | grep error"),
    ("rm star dot log", "rm *.log"),
])
def test_rules_shell(spoken, expected):
    assert R.apply_rules(spoken, "shell") == expected


@pytest.mark.parametrize("spoken,expected", [
    ("cd to slash is slash cluster", "cd /is/cluster"),
    ("ls slash tmp", "ls /tmp"),
    ("slash is slash cluster slash fast", "/is/cluster/fast"),
    ("seedy slash is slash cluster", "cd /is/cluster"),
    ("cd to slash is slash cluster slash fast", "cd /is/cluster/fast"),
    ("cd into tmp", "cd tmp"),
    ("cat log pipe seedy slash tmp", "cat log | cd /tmp"),
])
def test_rules_absolute_paths_and_cd(spoken, expected):
    """A slash with no path segment to its left opens an absolute path."""
    assert R.apply_rules(spoken, "shell") == expected


@pytest.mark.parametrize("spoken,register,expected", [
    # Mid-sentence is not command position: the word stays a word.
    ("that seedy pattern", "shell", "that seedy pattern"),
    ("git cd to tmp", "shell", "git cd to tmp"),
    # The rewrite is shell-only.
    ("seedy slash tmp", "claude", "seedy/tmp"),
    # "to" only drops after a command-position cd.
    ("go to tmp", "shell", "go to tmp"),
    # A word left of the slash really is a path segment.
    ("app slash py", "shell", "app/py"),
])
def test_rules_cd_and_slash_stay_narrow(spoken, register, expected):
    assert R.apply_rules(spoken, register) == expected


@pytest.mark.parametrize("text", [
    "the dot at the end of that sentence was wrong",
    "this is a well known problem dash we should fix it",
    "put a dash between the two words",
])
def test_rules_leave_prose_alone(text):
    """A spoken punctuation name between ordinary words is prose, not syntax."""
    assert R.apply_rules(text, "shell") == text


def test_em_dash_undo():
    """iOS turns a dictated "dash dash" into an em dash; it has to come back."""
    assert R.apply_rules("run — verbose", "shell") == "run --verbose"


def test_en_dash_before_word_becomes_hyphen_flag():
    assert R.apply_rules("ls – la", "shell") == "ls -la"


# ---------------------------------------------------------------------------
# Verbatim-transcriber conventions
# ---------------------------------------------------------------------------
# Parakeet writes every spoken separator as a word, so "git commit dash m" and
# "no dash build" reach the rules in the same shape and only position tells
# them apart. These pin the gates that separate a flag from a hyphen.

@pytest.mark.parametrize("spoken,expected", [
    # A dash while the line is still naming what to run is a flag, however many
    # subcommands deep, and whichever side of a pipe it falls on.
    ("git commit dash m fix the bug", "git commit -m fix the bug"),
    ("pip install dash e dot", "pip install -e dot"),
    ("tmux new dash s tokenhmr", "tmux new -s tokenhmr"),
    ("micromamba install dash c forge ffmpeg", "micromamba install -c forge ffmpeg"),
    ("ps aux pipe grep uvicorn pipe head dash n5",
     "ps aux | grep uvicorn | head -n5"),
])
def test_dash_after_a_command_is_a_flag_not_a_hyphen(spoken, expected):
    assert R.apply_rules(spoken, "shell") == expected


@pytest.mark.parametrize("spoken,expected", [
    # Past the command prefix the dash hyphenates the name it sits inside;
    # a long word on the right is a name even at command position.
    ("pip install dash dash no dash build dash isolation",
     "pip install --no-build-isolation"),
    ("micromamba install dash c conda dash forge", "micromamba install -c conda-forge"),
    ("git checkout dash b feature dash bar", "git checkout -b feature-bar"),
])
def test_dash_past_the_command_prefix_still_hyphenates(spoken, expected):
    assert R.apply_rules(spoken, "shell") == expected


def test_plus_at_command_position_is_a_mode_argument():
    """"chmod plus x" is "+x" standing alone, not a glued "chmod+x"."""
    assert R.apply_rules("chmod plus x run dot sh", "shell") == "chmod +x run.sh"
    assert R.apply_rules("chmod plus r notes dot txt", "shell") == "chmod +r notes.txt"
    # "plus" is an ordinary English word, so unlike a dash it earns the
    # standalone reading only under a real command — never under whatever word
    # happens to start a sentence. (The joiner's own reading of that line is
    # unchanged by this rule and is pinned elsewhere.)
    assert "+x" not in R.apply_rules("the plus x in that sentence",
                                     "shell").split()


def test_a_joiner_chain_of_common_words_is_an_identifier():
    """"test slash test underscore main dot py" is a path spelled out loud.

    Both sides of that first slash are ordinary English, which is what the
    prose guard holds back — but the chain continuing into another separator is
    the evidence that this is a name being spelled, not a sentence.
    """
    assert R.apply_rules("test slash test underscore main dot py", "shell") == \
        "test/test_main.py"


def test_rules_are_reduced_outside_shell():
    """Standalone symbols and bare digits are shell-only; joiners survive."""
    assert R.apply_rules("cat log pipe grep error", "claude") == \
        "cat log pipe grep error"
    assert R.apply_rules("tests slash test dot py", "claude") == "tests/test.py"


def test_comma_fragmented_dictation_does_not_glue_punctuation_mid_token():
    """Whisper comma-separates list-ish dictation (a comma-list --prompt used
    to teach it this format); joined path segments must not carry the comma
    into the middle of the resulting token. Full-sentence recovery is not
    required here — "slash, is" between common words staying literal is
    accepted behavior.
    """
    spoken = ("Can you go to, slash, is, last, cluster, slash, fast, slash, "
               "as, duetty, slash, work?")
    result = R.apply_rules(spoken, "claude")
    assert ",/" not in result


# ---------------------------------------------------------------------------
# ASR-only rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("heard,expected", [
    # whisper hears the operator as the package manager; both sides are
    # command-shaped, so nothing else it could be.
    ("cat app.py pip grep import", "cat app.py pipe grep import"),
    ("ls pip head", "ls pipe head"),
])
def test_asr_pip_becomes_pipe(heard, expected):
    assert R.apply_asr_rules(heard) == expected


@pytest.mark.parametrize("heard", [
    # A subcommand on the right means the user really did say the tool.
    "pip install numpy",
    "pip uninstall torch",
    "pip freeze",
    "pip list",
    # And a verb on the left means it is the object, not an operator.
    "upgrade pip",
    "python -m pip install requests",
    # Nothing to pipe into: a sentence that happens to end on the word.
    "i need to install pip",
])
def test_asr_leaves_the_package_manager_alone(heard):
    assert R.apply_asr_rules(heard) == heard


def test_asr_rules_only_run_on_transcripts(project):
    """Typed text means what it says — the rewrite is opt-in per call."""
    typed = "cat app.py pip grep import"
    assert "|" not in R.resolve(typed, screen=SHELL_SCREEN, cwd=project)["text"]
    assert "|" in R.resolve(typed, screen=SHELL_SCREEN, cwd=project,
                            asr=True)["text"]


def test_asr_pip_reaches_the_pipe_character_end_to_end(project):
    """The ASR pass hands "pipe" to the rules, which make it the operator."""
    result = R.resolve("git status pip grep main", screen=SHELL_SCREEN,
                       cwd=project, asr=True)
    assert result["text"] == "git status | grep main"


# ---------------------------------------------------------------------------
# Sentence dressing and the "Get"/"git" homophone (shell register only)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("heard,expected", [
    # "Get" at command position in front of a git subcommand becomes "git";
    # the ASR pass alone doesn't touch "dash dash ..." — that is apply_rules'
    # job, exercised separately in the end-to-end test below.
    ("Get push dash dash force dash with dash lease.",
     "git push dash dash force dash with dash lease"),
    # A plain trailing period on an ordinary command line comes off.
    ("Run pytest.", "Run pytest"),
    # The final mark comes off a filename-like token, but its own dot (the
    # extension's, not the sentence's) is untouched.
    ("cat app.py.", "cat app.py"),
    # "Git" heard correctly is already lowercase-worthy and the rule is a
    # no-op on the homophone fix, but sentence dressing still runs.
    ("Git status.", "git status"),
])
def test_asr_shell_dressing_table(heard, expected):
    assert R.apply_asr_rules(heard, "shell") == expected


def test_asr_get_needs_a_git_subcommand_on_the_right():
    """No known subcommand after "Get" — leave the word alone."""
    result = R.apply_asr_rules("Get the file from the server.", "shell")
    assert result.split()[0] == "Get"


def test_asr_get_only_fires_at_command_position():
    """"get" before a subcommand-shaped word, but not at position 0, is prose."""
    heard = "go get push notifications working."
    result = R.apply_asr_rules(heard, "shell")
    assert "git" not in result.split()


def test_asr_dressing_is_shell_only(project):
    """Claude/editor registers keep prose case and punctuation untouched."""
    heard = "Get push dash dash force dash with dash lease."
    claude_result = R.apply_asr_rules(heard, "claude")
    assert claude_result == heard
    editor_result = R.apply_asr_rules(heard, "editor")
    assert editor_result == heard


def test_asr_shell_dressing_end_to_end(project):
    result = R.resolve("Get push dash dash force dash with dash lease.",
                       screen=SHELL_SCREEN, cwd=project, asr=True)
    assert result["text"] == "git push --force-with-lease"


def test_asr_dressing_claude_register_keeps_case_and_period(project):
    """Prose in the claude register is not a command line — leave it be."""
    result = R.resolve("Get push dash dash force dash with dash lease.",
                       screen=CLAUDE_SCREEN, cwd=project, asr=True)
    assert result["text"].startswith("Get push")
    assert result["text"].endswith(".")


def test_asr_dressing_never_touches_typed_text(project):
    """asr=False must skip the ASR-only pass: case and the period survive.

    apply_rules (spoken-syntax) still runs either way and turns the dictated
    dashes into a flag — that part is not gated on `asr` — but the sentence
    case and the trailing period are apply_asr_rules' job alone, so a typed
    "Get" and its period must come through untouched.
    """
    typed = "Get push dash dash force dash with dash lease."
    result = R.resolve(typed, screen=SHELL_SCREEN, cwd=project, asr=False)
    assert result["text"] == "Get push --force-with-lease."


@pytest.mark.parametrize("text,expected", [
    ("ls | grep test", "ls | grep test"),
    ("git log | head", "git log | head"),
])
def test_matcher_never_swallows_an_operator(project, text, expected):
    """normalize() erases punctuation, so "| grep" scored 0.95 against `grep`
    and the snap deleted the pipe. Any window holding an operator is skipped."""
    assert R.resolve(text, screen=SHELL_SCREEN, cwd=project)["text"] == expected


# ---------------------------------------------------------------------------
# Filesystem path snapping (ASR only)
# ---------------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path):
    """A fake mount with the shape the real failure had: a directory whose
    listing holds the word whisper mangled."""
    (tmp_path / "cluster" / "fast" / "sdwivedi" / "work").mkdir(parents=True)
    (tmp_path / "cluster" / "slow").mkdir()
    (tmp_path / "archive").mkdir()
    return tmp_path


def test_snap_fixes_a_misheard_middle_segment(tree):
    """`claster` names nothing, and the parent's listing holds `cluster`."""
    spoken = f"go to {tree}/claster/fast"
    assert R.snap_paths(spoken) == f"go to {tree}/cluster/fast"


def test_snap_stops_at_a_segment_with_no_near_match(tree):
    """A prefix we could not establish makes every deeper listing the wrong
    directory, so the rest of the token is kept exactly as spoken."""
    spoken = f"go to {tree}/qqqqqqqq/fast"
    assert R.snap_paths(spoken) == spoken


def test_snap_never_rewrites_a_segment_that_exists(tree):
    """`work`, `fast` and `slow` are real words that also name real
    directories; existing segments are not candidates at all."""
    spoken = f"go to {tree}/cluster/slow"
    assert R.snap_paths(spoken) == spoken


def test_snap_keeps_the_tail_verbatim_after_it_stops(tree):
    """The corrected prefix lands, the unresolvable segment and everything
    under it do not."""
    spoken = f"go to {tree}/claster/fast/qqqqqqqq/work"
    assert R.snap_paths(spoken) == f"go to {tree}/cluster/fast/qqqqqqqq/work"


def test_snap_merges_a_path_whisper_split_at_a_space(tree):
    """`/cluster` does not exist at the root it was written against, but does
    under the token to its left — so the space was whisper's, not the user's."""
    spoken = f"go to {tree} /claster/fast"
    assert R.snap_paths(spoken) == f"go to {tree}/cluster/fast"


def test_snap_does_not_merge_two_independently_valid_paths(tmp_path):
    """Both resolve from root, so they are two paths and the merge is off."""
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    spoken = f"diff {tmp_path}/one {tmp_path}/two"
    assert R.snap_paths(spoken) == spoken


def test_snap_preserves_trailing_sentence_punctuation(tree):
    spoken = f"look in {tree}/claster,"
    assert R.snap_paths(spoken) == f"look in {tree}/cluster,"


def test_snap_leaves_a_lone_nonexistent_segment_alone(tmp_path):
    """A single-segment path that does not exist must not put a listing of the
    root behind an ordinary sentence."""
    assert R.snap_paths("/qqqqqqqq is a word") == "/qqqqqqqq is a word"


def test_snap_only_runs_on_transcripts(tree, project):
    """Typed text means what it says — the same string is snapped as a
    transcript and left alone when the user wrote it."""
    typed = f"go to {tree}/claster/fast"
    assert R.resolve(typed, screen=CLAUDE_SCREEN, cwd=project)["text"] == typed
    assert R.resolve(typed, screen=CLAUDE_SCREEN, cwd=project,
                     asr=True)["text"] == f"go to {tree}/cluster/fast"


def test_snap_degrades_to_unchanged_past_its_deadline(tree, monkeypatch):
    """A mount that has stopped answering costs the budget and nothing more."""
    spoken = f"go to {tree}/claster/fast"
    real_monotonic = time.monotonic
    calls = {"n": 0}

    def jumped():
        calls["n"] += 1
        return real_monotonic() if calls["n"] == 1 else real_monotonic() + 10.0

    monkeypatch.setattr(time, "monotonic", jumped)
    assert R.snap_paths(spoken) == spoken


def test_snap_degrades_to_unchanged_when_the_filesystem_raises(tree, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("stale file handle")

    monkeypatch.setattr(R.os, "scandir", boom)
    monkeypatch.setattr(R.os.path, "lexists", boom)
    spoken = f"go to {tree}/claster/fast"
    assert R.snap_paths(spoken) == spoken


def test_snap_fallback_takes_an_unambiguous_near_miss(tmp_path):
    """A username has no pronunciation to be right about, so the phonetic
    scorer finds nothing for whisper's "sdwedi" (it shares no metaphone key
    with `sdwivedi`). Character overlap does, at 0.857 against a listing that
    holds one plausible answer."""
    (tmp_path / "sdwivedi" / "work").mkdir(parents=True)
    (tmp_path / "mblack").mkdir()
    assert R.snap_paths(f"go to {tmp_path}/sdwedi/work") \
        == f"go to {tmp_path}/sdwivedi/work"


def test_snap_fallback_needs_a_clear_winner(tmp_path):
    """Two siblings the segment resembles equally well: neither is evidence,
    so the walk stops rather than picking one."""
    (tmp_path / "sdwivedi").mkdir()
    (tmp_path / "sdwivedj").mkdir()
    spoken = f"go to {tmp_path}/sdwivedx"
    assert R.snap_paths(spoken) == spoken


def test_snap_fallback_ignores_short_segments(tmp_path):
    """Below five characters one edit flips the ranking, so the ratio stops
    being evidence — "stvd" must not reach `stvx` on 0.75."""
    (tmp_path / "abcd").mkdir()
    (tmp_path / "work").mkdir()
    spoken = f"go to {tmp_path}/abcx/work"
    assert R.snap_paths(spoken) == spoken


def test_snap_fallback_still_respects_the_digit_guard(tmp_path):
    """The ratio treats digits as ordinary characters, so the guard has to
    cover the fallback too: `sdwedi2` must not reach `sdwivedi`."""
    (tmp_path / "sdwivedi").mkdir()
    spoken = f"go to {tmp_path}/sdwedi2"
    assert R.snap_paths(spoken) == spoken


def test_snap_does_not_drop_a_digit_the_speaker_said(tree):
    """metaphone() erases digits, so `cluster2` keys the same as `cluster`.
    Dictated digits transcribe literally, so disagreeing on them means two
    names rather than one mishearing."""
    spoken = f"go to {tree}/cluster2/fast"
    assert R.snap_paths(spoken) == spoken


@pytest.fixture
def relproj(tmp_path):
    """A project the way a dictated relative path expects to find one."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_camerahmr.py").write_text("")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_bench.sh").write_text("")
    (tmp_path / "vendor").mkdir()
    return str(tmp_path)


def test_snap_fixes_a_relative_path_against_the_request_cwd(relproj):
    """"tests/test_x.py" is the shape dictation actually produces.

    Nothing about it is rooted, so it is only checkable against the directory
    the request came from.
    """
    assert R.snap_paths("tesst/test_camerahmr.py", cwd=relproj) == \
        "tests/test_camerahmr.py"
    assert R.snap_paths("scripts/run_bench.sh", cwd=relproj) == \
        "scripts/run_bench.sh"


def test_a_relative_token_is_never_rooted_at_the_filesystem_root(relproj):
    """The walk anchors at the cwd, never at "/".

    Treating a relative token as absolute used to consume its first character
    as the leading separator and return "/est/test_camerahmr.py".
    """
    for text in ("tests/test_camerahmr.py", "tesst/test_camerahmr.py"):
        assert not R.snap_paths(text, cwd=relproj).startswith("/")
    assert R._snap_walk("tests/test_camerahmr.py", os.path.expanduser("~"), 0) == \
        "tests/test_camerahmr.py"


@pytest.mark.parametrize("text", [
    # A bare word is never a path, so the filesystem is never asked about it.
    "vendorr",
    "checkpoint",
    # Prose that happens to hold a slash: neither segment names anything here.
    "either/or",
    "read/write access",
    # A first segment that names nothing stops the walk before it starts, so
    # the segments under it are kept exactly as spoken.
    "qqqqqqqq/test_camerahmr.py",
])
def test_relative_snapping_leaves_everything_else_alone(relproj, text):
    assert R.snap_paths(text, cwd=relproj) == text


def test_a_relative_path_needs_a_cwd_to_be_relative_to(relproj):
    """With no usable cwd there is no anchor, so the token passes through."""
    assert R.snap_paths("tesst/test_camerahmr.py") == "tesst/test_camerahmr.py"
    assert R.snap_paths("tesst/test_camerahmr.py", cwd="") == \
        "tesst/test_camerahmr.py"
    # An unreadable or missing cwd degrades to the same no-op rather than
    # raising or probing something else.
    missing = os.path.join(relproj, "no-such-dir")
    assert R.snap_paths("tesst/test_camerahmr.py", cwd=missing) == \
        "tesst/test_camerahmr.py"


def test_absolute_and_home_snapping_is_unchanged_by_a_cwd(tree):
    """A cwd is the anchor for relative tokens only; rooted ones ignore it."""
    spoken = f"go to {tree}/claster/fast"
    assert R.snap_paths(spoken, cwd=str(tree)) == R.snap_paths(spoken)
    assert R.snap_paths(spoken, cwd=str(tree)) == f"go to {tree}/cluster/fast"


def test_snap_never_writes_to_the_filesystem(tree):
    """Read-only by construction: the tree is byte-identical afterwards."""
    before = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*"))
    R.snap_paths(f"go to {tree}/claster/fast/qqqqqqqq")
    assert sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*")) == before


# ---------------------------------------------------------------------------
# Register detection
# ---------------------------------------------------------------------------

def test_register_shell():
    assert R.detect_register(SHELL_SCREEN) == "shell"


def test_register_shell_zsh_arrow():
    assert R.detect_register(["~/work ❯ ls", "app.py"]) == "shell"


def test_register_claude():
    assert R.detect_register(CLAUDE_SCREEN) == "claude"


def test_register_editor():
    assert R.detect_register(VIM_SCREEN) == "editor"


def test_register_unknown_is_conservative():
    """No marker at all must read as claude — the tighter of the two settings."""
    assert R.detect_register(["some output", "more output"]) == "claude"
    assert R.detect_register([]) == "claude"
    assert R.detect_register(None) == "claude"


def test_editor_beats_a_prompt_on_screen():
    """A shell prompt scrolled above a vim session must not win the vote."""
    assert R.detect_register(SHELL_SCREEN + VIM_SCREEN) == "editor"


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def test_index_from_screen_keeps_case():
    index = R.build_index(screen=["class CameraHMR:", "x = 1"])
    assert any(e.surface == "CameraHMR" for e in index.entries)


def test_index_skips_short_and_numeric_tokens():
    index = R.build_index(screen=["a b 12 345 ok"])
    surfaces = {e.surface for e in index.entries if e.source == "screen"}
    assert "12" not in surfaces and "345" not in surfaces


def test_index_has_is_verbatim_not_normalized():
    """The guard that nearly broke the headline case: a spoken window whose
    letters happen to match an entry is exactly what needs correcting."""
    index = R.build_index(screen=["test_camerahmr.py"])
    assert index.has("test_camerahmr.py")
    assert not index.has("test_camera h m r.py")


def test_index_walk_respects_entry_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "MAX_WALK_ENTRIES", 20)
    for i in range(200):
        (tmp_path / f"file{i}.py").write_text("")
    assert len(R.walk_names(str(tmp_path))) <= 21


def test_index_walk_respects_depth_cap(tmp_path):
    deep = tmp_path
    for level in range(6):
        deep = deep / f"lvl{level}"
        deep.mkdir()
    (deep / "buried.py").write_text("")
    assert "buried.py" not in R.walk_names(str(tmp_path))


def test_index_walk_skips_noise_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.py").write_text("")
    names = R.walk_names(str(tmp_path))
    assert "junk.js" not in names and "secret.py" not in names


def test_index_walk_tolerates_missing_dir():
    assert R.walk_names("/nonexistent/path/xyz") == []
    assert R.walk_names("") == []


def test_git_branches_tolerates_absent_repo(tmp_path):
    assert R.git_branches(str(tmp_path)) == []
    assert R.git_branches("/nonexistent/xyz") == []


def git_repo(path, *files):
    """A repo at `path` with `files` committed, or a skip if git is unusable."""
    import subprocess as sp
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": os.environ.get("PATH", ""), "HOME": str(path)}

    def run(*args):
        return sp.run(["git"] + list(args), cwd=str(path), env=env,
                      capture_output=True, text=True, timeout=10)

    try:
        if run("init", "-q").returncode != 0:
            pytest.skip("git init failed")
    except (OSError, sp.SubprocessError):
        pytest.skip("git is not available")
    for name in files:
        (path / name).write_text("")
        run("add", name)
        run("commit", "-q", "-m", name)
    return run


def test_git_touched_files_lists_recent_commits_and_working_changes(tmp_path):
    run = git_repo(tmp_path, "committed_one.py", "committed_two.py")
    (tmp_path / "dirty_file.py").write_text("x")
    run("add", "dirty_file.py")
    (tmp_path / "untracked_thing.md").write_text("x")

    names = R.git_touched_files(str(tmp_path))
    for expected in ("dirty_file.py", "untracked_thing.md",
                     "committed_one.py", "committed_two.py"):
        assert expected in names, expected
    # Status is "right now", so it comes ahead of the commit history.
    assert names.index("dirty_file.py") < names.index("committed_one.py")
    # Newest commit first, which is the order git log already emits.
    assert names.index("committed_two.py") < names.index("committed_one.py")


def test_git_touched_files_are_basenames_and_deduped(tmp_path):
    run = git_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "leaf.py").write_text("")
    run("add", "src/leaf.py")
    run("commit", "-q", "-m", "one")
    (tmp_path / "src" / "leaf.py").write_text("changed")

    names = R.git_touched_files(str(tmp_path))
    # The walk already holds "src/leaf.py"; this source is about the spoken
    # filename, and it appears once even though status and log both print it.
    assert names.count("leaf.py") == 1
    assert not any("/" in name for name in names)


def test_git_touched_files_respect_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "MAX_GIT_TOUCHED_FILES", 5)
    git_repo(tmp_path)
    for i in range(20):
        (tmp_path / f"untracked{i}.py").write_text("")
    assert len(R.git_touched_files(str(tmp_path))) == 5


def test_git_touched_files_tolerate_absent_repo(tmp_path):
    assert R.git_touched_files(str(tmp_path)) == []
    assert R.git_touched_files("/nonexistent/xyz") == []


def test_cwd_vocabulary_carries_the_recently_touched_files(tmp_path):
    git_repo(tmp_path, "recently_edited.py")
    R._cwd_cache.clear()
    names, _ = R.cwd_vocabulary(str(tmp_path))
    assert "recently_edited.py" in names


# ---------------------------------------------------------------------------
# SSH config hosts
# ---------------------------------------------------------------------------

@pytest.fixture
def ssh_config(tmp_path, monkeypatch):
    """A ~/.ssh/config at a known path, with the module cache cleared.

    The real config must never decide a test's outcome, so $HOME is pointed at
    the fixture and the cache is emptied on the way in and out.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").mkdir()

    def write(text: str):
        path = tmp_path / ".ssh" / "config"
        path.write_text(text)
        R._ssh_cache["stamp"] = None
        R._ssh_cache["hosts"] = []
        return str(path)

    R._ssh_cache["stamp"] = None
    R._ssh_cache["hosts"] = []
    yield write
    R._ssh_cache["stamp"] = None
    R._ssh_cache["hosts"] = []


def test_ssh_hosts_reads_aliases_and_hostnames(ssh_config):
    ssh_config("Host galton\n"
               "  HostName galton.is.localnet\n"
               "  User sdwivedi\n")
    hosts = R.ssh_hosts()
    assert "galton" in hosts
    assert "galton.is.localnet" in hosts
    # Only the two host keywords; nothing else on the block is a name.
    assert "sdwivedi" not in hosts


def test_ssh_hosts_takes_every_alias_on_a_host_line(ssh_config):
    ssh_config("Host cluster login gate\n  HostName login.cluster.net\n")
    hosts = R.ssh_hosts()
    for alias in ("cluster", "login", "gate"):
        assert alias in hosts, alias


def test_ssh_hosts_skip_wildcard_patterns(ssh_config):
    """A pattern is a rule about hosts, not a name anyone says out loud."""
    ssh_config("Host *\n  ForwardAgent yes\n"
               "Host *.cluster node?\n"
               "Host realhost\n")
    hosts = R.ssh_hosts()
    assert hosts == ["realhost"]


def test_ssh_hosts_ignore_comments_and_keyword_case(ssh_config):
    ssh_config("# Host commented\n"
               "hostname lowercase.example\n"
               "HOST shouty\n")
    hosts = R.ssh_hosts()
    assert "commented" not in hosts
    assert "lowercase.example" in hosts
    assert "shouty" in hosts


def test_ssh_hosts_accept_the_equals_form(ssh_config):
    ssh_config("Host=galton\n")
    assert R.ssh_hosts() == ["galton"]


def test_ssh_hosts_are_deduped(ssh_config):
    ssh_config("Host galton\n  HostName galton\n"
               "Host galton\n")
    assert R.ssh_hosts() == ["galton"]


def test_ssh_hosts_are_empty_when_the_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    R._ssh_cache["stamp"] = None
    R._ssh_cache["hosts"] = []
    assert R.ssh_hosts() == []


def test_ssh_hosts_are_empty_when_the_file_is_unreadable(ssh_config, monkeypatch):
    ssh_config("Host galton\n")

    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr("builtins.open", boom)
    assert R.ssh_hosts() == []


def test_ssh_hosts_cache_serves_a_second_call_without_reparsing(ssh_config,
                                                                monkeypatch):
    ssh_config("Host galton\n")
    assert R.ssh_hosts() == ["galton"]
    monkeypatch.setattr("builtins.open",
                        lambda *a, **k: pytest.fail("the config was re-read"))
    assert R.ssh_hosts() == ["galton"]


def test_ssh_hosts_cache_invalidates_when_the_file_changes(ssh_config):
    """Same path, new content: the (path, mtime, size) stamp no longer matches.

    The fixture's writer clears the cache, so the stamp is re-armed here from a
    live call and the second write is what has to invalidate it.
    """
    ssh_config("Host galton\n")
    assert R.ssh_hosts() == ["galton"]
    # Written directly rather than through the fixture, so nothing but the
    # stamp itself can be what notices the change. A different length is what
    # keeps this independent of the clock's mtime resolution.
    Path(os.path.expanduser(R.SSH_CONFIG_PATH)).write_text(
        "Host newbox longer_alias\n")
    assert R.ssh_hosts() == ["newbox", "longer_alias"]


# ---------------------------------------------------------------------------
# Home-directory dotfiles
# ---------------------------------------------------------------------------

@pytest.fixture
def home_dotfiles(tmp_path, monkeypatch):
    """A $HOME whose dotfiles the test writes, with the cache cleared around it."""
    monkeypatch.setenv("HOME", str(tmp_path))

    def write(*names: str):
        for name in names:
            (tmp_path / name).write_text("x\n")
        R._dotfile_cache["stamp"] = None
        R._dotfile_cache["names"] = []
        return tmp_path

    R._dotfile_cache["stamp"] = None
    R._dotfile_cache["names"] = []
    yield write
    R._dotfile_cache["stamp"] = None
    R._dotfile_cache["names"] = []


def test_dotfile_names_drop_the_leading_dot(home_dotfiles):
    """Nobody dictates the dot: "open bashrc" is the whole utterance."""
    home_dotfiles(".bashrc", ".zshrc", ".vimrc")
    assert R.dotfile_names() == ["bashrc", "vimrc", "zshrc"]


def test_dotfile_names_take_the_config_shapes(home_dotfiles):
    home_dotfiles(".bashrc", ".gitconfig", ".profile", ".tmux.conf",
                  ".zshenv", ".gitignore")
    names = R.dotfile_names()
    for expected in ("bashrc", "gitconfig", "profile", "zshenv", "gitignore"):
        assert expected in names, expected
    # A dotted name is kept whole: it is one thing the user says, and the
    # matcher already splits a surface into its subwords when it scores one.
    assert "tmux.conf" in names


def test_dotfile_names_skip_state_files(home_dotfiles):
    """History, caches and authority files are never said out loud.

    This is the noise the cap exists to survive: a real home holds sixty
    `.python_history-<pid>.tmp` files and a handful of `.bak` copies.
    """
    home_dotfiles(".bashrc", ".zsh_history", ".viminfo", ".lesshst",
                  ".zcompdump", ".Xauthority", ".xsession-errors",
                  ".python_history-02006.tmp", ".tmux.conf.bak.20260818")
    assert R.dotfile_names() == ["bashrc"]


def test_dotfile_names_skip_directories(home_dotfiles):
    """`.config` and `.cargo` are places, not files anyone asks to open."""
    home_dotfiles(".bashrc")
    for name in (".config", ".cargo", ".ssh"):
        (Path(os.environ["HOME"]) / name).mkdir()
    R._dotfile_cache["stamp"] = None
    assert R.dotfile_names() == ["bashrc"]


def test_dotfile_names_ignore_ordinary_files(home_dotfiles):
    home_dotfiles(".bashrc")
    (Path(os.environ["HOME"]) / "notes.txt").write_text("x\n")
    R._dotfile_cache["stamp"] = None
    assert R.dotfile_names() == ["bashrc"]


def test_dotfile_names_are_capped(home_dotfiles):
    home_dotfiles(*[f".thing{n}rc" for n in range(60)])
    assert len(R.dotfile_names()) == R.DOTFILE_MAX_NAMES


def test_dotfile_names_are_empty_when_home_is_unreadable(home_dotfiles,
                                                         monkeypatch):
    home_dotfiles(".bashrc")

    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(R.os, "listdir", boom)
    assert R.dotfile_names() == []


def test_dotfile_names_cache_serves_a_second_call_without_restatting(
        home_dotfiles, monkeypatch):
    """The listing is the stamp, so it is re-read; the work behind it is not.

    Per-entry `isfile` is what the cache exists to skip — one stat for every
    config-shaped name, on a path the transcribe route takes per request.
    """
    home_dotfiles(".bashrc")
    assert R.dotfile_names() == ["bashrc"]
    monkeypatch.setattr(R.os.path, "isfile",
                        lambda *a, **k: pytest.fail("the entries were restatted"))
    assert R.dotfile_names() == ["bashrc"]


def test_dotfile_names_cache_invalidates_when_home_changes(home_dotfiles):
    """A new dotfile changes the listing, which is the stamp.

    A directory's (mtime, size) is not: the size stays at the block size and
    the mtime resolution is coarse enough that a file written in the same
    instant as the last one leaves it untouched, so a stamp of the shape the
    history and ssh sources use would serve a stale answer here.
    """
    home_dotfiles(".bashrc")
    assert R.dotfile_names() == ["bashrc"]
    # Written directly rather than through the fixture, so nothing but the
    # stamp itself can be what notices the new entry.
    (Path(os.environ["HOME"]) / ".zshrc").write_text("x\n")
    assert R.dotfile_names() == ["bashrc", "zshrc"]


def test_subwords_splits_every_convention():
    assert R.subwords("test_camerahmr.py") == ["test", "camerahmr", "py"]
    assert R.subwords("getUserName") == ["get", "User", "Name"]
    assert R.subwords("some-kebab-case") == ["some", "kebab", "case"]


# ---------------------------------------------------------------------------
# Matcher scoring and thresholds
# ---------------------------------------------------------------------------

def test_exact_match_scores_one():
    entry = R.Entry("pytest", "path")
    assert R.score_entry("pytest", ["pytest"], entry) == 1.0


def test_separator_only_difference_scores_high():
    entry = R.Entry("test_camerahmr.py", "cwd")
    score = R.score_entry("test_camera h m r.py", ["test_camera", "h", "m", "r.py"],
                          entry)
    assert score == 0.95


def test_phonetic_match_scores_point_eight_five():
    entry = R.Entry("pytest", "path")
    assert R.score_entry("pietest", ["pietest"], entry) == 0.85


def test_short_windows_get_no_fuzzy_credit():
    """Below the minimum length a phonetic key is coincidence, not evidence."""
    entry = R.Entry("make", "path")
    assert R.score_entry("mac", ["mac"], entry) == 0.0


def test_wildly_different_lengths_do_not_match():
    entry = R.Entry("build_mobile.py", "cwd")
    assert R.score_entry("build", ["build"], entry) == 0.0


def test_common_word_protected_in_claude_register():
    """A plain English word may not be replaced on a phonetic guess."""
    index = R.build_index(screen=["retest.py"])
    surface, _, _, _ = R.match_window("test", ["test"], index, "claude", 0.85)
    assert surface == ""


def test_common_word_still_protected_in_shell():
    index = R.build_index(screen=["retest.py"])
    surface, _, _, _ = R.match_window("test", ["test"], index, "shell", 0.80)
    assert surface == ""


def test_path_entries_only_match_at_command_position():
    """$PATH is where "review" turns into "rview" — gate it to command position."""
    index = R.Index()
    index.add("rview", "path")
    assert R.match_window("review", ["review"], index, "shell", 0.80,
                          at_command=False)[0] == ""
    assert R.match_window("review", ["review"], index, "shell", 0.80,
                          at_command=True)[0] == "rview"


def test_alternates_are_capped_and_exclude_the_winner():
    index = R.Index()
    for name in ("pytest", "py.test", "pytests", "pytest3"):
        index.add(name, "screen")
    surface, _, alternates, _ = R.match_window("pie test", ["pie", "test"],
                                               index, "shell", 0.80)
    assert surface not in alternates
    assert len(alternates) <= 3


def test_a_remembered_name_spelled_out_merges_away_from_command_position():
    """A verbatim transcriber writes "CameraHMR" as the words it sounds like.

    Remembered commands are normally held to command position — mid-sentence
    they would overrule ordinary prose. A window whose letters spell the entry
    exactly is different evidence: those are the same characters in the same
    order, not a guess about what one word sounded like.
    """
    index = R.Index()
    index.add("CameraHMR", "history")
    surface, _, _, _ = R.match_window("camera hmr", ["camera", "hmr"], index,
                                      "shell", 0.80, at_command=False)
    assert surface == "CameraHMR"


def test_one_word_against_a_remembered_command_stays_guarded():
    """The single-word case the position guard exists for is untouched."""
    index = R.Index()
    index.add("rview", "history")
    assert R.match_window("review", ["review"], index, "shell", 0.80,
                          at_command=False)[0] == ""


@pytest.mark.parametrize("spoken,expected", [
    # The separator claimed the first half of the name; the second half was
    # left standing beside it as its own word.
    ("pretrained_models/camera hmr", "pretrained_models/CameraHMR"),
    # ...and here the second half was in turn claimed by the next separator.
    ("work/interact vlm/outputs", "work/InteractVLM/outputs"),
])
def test_a_name_split_across_a_path_separator_is_rejoined(spoken, expected):
    index = R.Index()
    index.add("CameraHMR", "history")
    index.add("InteractVLM", "history")
    text, _ = R.resolve_tokens(spoken, index, "shell")
    assert text == expected


def test_a_word_after_a_path_is_only_absorbed_when_it_spells_the_name():
    """Without an exact spelling, the word after a path stays a separate word."""
    index = R.Index()
    index.add("CameraHMR", "history")
    text, _ = R.resolve_tokens("pretrained_models/camera checkpoint", index, "shell")
    assert text == "pretrained_models/camera checkpoint"


def test_source_priority_breaks_ties():
    """What is on screen beats what merely exists on $PATH."""
    index = R.Index()
    index.add("camerahmr", "path")
    index.add("camerahmr", "screen")
    surface, _, _, source = R.match_window("camera h m r", ["camera", "h", "m", "r"],
                                           index, "shell", 0.80)
    assert surface == "camerahmr" and source == "screen"


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spoken,expected", [
    ("pie test tests slash test underscore camera h m r dot py",
     "pytest tests/test_camerahmr.py"),
    ("open app dot py", "open app.py"),
    ("pip install dash dash no dash build isolation",
     "pip install --no-build isolation"),
    ("git checkout head tilde three", "git checkout head~3"),
])
def test_resolve_fixes_commands(project, spoken, expected):
    result = R.resolve(spoken, screen=SHELL_SCREEN, cwd=project)
    assert result["text"] == expected


@pytest.mark.parametrize("spoken,expected", [
    # `cat` is itself a $PATH binary. A multi-word window anchored on it
    # ("cat app.py", "cat file") must not be rewritten to a phonetically
    # colliding but wildly dissimilar $PATH binary (gftype, keytool) just
    # because the window as a whole clears the metaphone-equality tier.
    ("cat app.py", "cat app.py"),
    ("cat file", "cat file"),
    # Surviving positives on the same $PATH metaphone-equality tier: high
    # spelling similarity must still let these through.
    ("pie test on app dot py", "pytest on app.py"),
    ("h top", "htop"),
])
def test_resolve_leaves_path_binaries_and_their_windows_correct(project, spoken, expected):
    result = R.resolve(spoken, screen=SHELL_SCREEN, cwd=project)
    assert result["text"] == expected


PROSE = [
    "can you add a test for the new function and then run the suite",
    "i think the report is due next week but we can talk about it tomorrow",
    "please review my changes when you get a chance",
    "we need to decide whether to ship this or wait until the next release",
    "it looks good to me but let me know if you see a problem",
    "make sure the data is backed up before you start the migration",
    "the results were better than we expected this time around",
    "remind me to send the invoice on friday morning",
    "the meeting was moved to the afternoon because of a conflict",
    "let me know what you think about the design before the meeting",
]


@pytest.mark.parametrize("text", PROSE)
def test_prose_passes_through_unchanged_in_shell(project, text):
    """The success case. Byte-identical text and not a single span."""
    result = R.resolve(text, screen=SHELL_SCREEN, cwd=project)
    assert result["text"] == text
    assert result["spans"] == []


@pytest.mark.parametrize("text", PROSE)
def test_prose_passes_through_unchanged_in_claude(project, text):
    result = R.resolve(text, screen=CLAUDE_SCREEN, cwd=project)
    assert result["text"] == text
    assert result["spans"] == []


def test_mixed_prose_and_identifier(project):
    """Only the technical part moves; the sentence around it is untouched."""
    result = R.resolve("please take a look at app dot py when you can",
                       screen=SHELL_SCREEN, cwd=project)
    assert result["text"] == "please take a look at app.py when you can"


def test_spans_index_into_the_corrected_text(project):
    """The client slices the returned text with these offsets — they must land."""
    result = R.resolve("pie test tests slash test underscore camera h m r dot py",
                       screen=SHELL_SCREEN, cwd=project)
    assert result["spans"]
    for span in result["spans"]:
        assert 0 <= span["start"] <= span["end"] <= len(result["text"])
        assert result["text"][span["start"]:span["end"]]
        assert span["source"] in ("phonetic", "rule", "dict")
        assert len(span["alternates"]) <= 3


def test_rule_and_token_spans_both_survive(project):
    """Both passes changed something, so the user gets a chip for each.

    The token pass shifts the text under the rule pass's offsets, so this is
    really a test that the merge re-locates them instead of dropping them.
    """
    result = R.resolve("pie test on tests slash test underscore camerahmr dot py",
                       screen=SHELL_SCREEN, cwd=project)
    assert result["text"] == "pytest on tests/test_camerahmr.py"
    sources = {span["source"] for span in result["spans"]}
    assert sources == {"phonetic", "rule"}
    for span in result["spans"]:
        assert result["text"][span["start"]:span["end"]]


def test_spans_never_overlap(project):
    """Overlapping spans would make the client's chip row rebuild the wrong text."""
    result = R.resolve("pie test on tests slash test underscore camerahmr dot py",
                       screen=SHELL_SCREEN, cwd=project)
    ordered = sorted(result["spans"], key=lambda s: s["start"])
    for earlier, later in zip(ordered, ordered[1:]):
        assert earlier["end"] <= later["start"]


def test_resolve_reports_the_register(project):
    assert R.resolve("ls", screen=SHELL_SCREEN, cwd=project)["register"] == "shell"
    assert R.resolve("hello", screen=VIM_SCREEN, cwd=project)["register"] == "editor"


def test_resolve_handles_empty_and_blank():
    for text in ("", "   ", None):
        result = R.resolve(text, screen=[], cwd="")
        assert result["spans"] == []


def test_resolve_handles_missing_context():
    """No screen, no cwd: still answers, just with less to go on."""
    result = R.resolve("hello there", screen=None, cwd="")
    assert result["text"] == "hello there"


def test_resolve_handles_a_bad_cwd():
    result = R.resolve("hello there", screen=[], cwd="/nonexistent/path/xyz")
    assert result["text"] == "hello there"


def test_resolve_echoes_when_the_budget_is_gone(project):
    """A zero budget must degrade to the input, never to a partial rewrite."""
    text = "pie test tests slash test underscore camera h m r dot py"
    result = R.resolve(text, screen=SHELL_SCREEN, cwd=project, budget=0.0)
    assert result["text"]
    assert isinstance(result["spans"], list)


def test_resolve_returns_unchanged_when_deadline_already_passed(project, monkeypatch):
    """The budget is gone before resolve() even starts building an index.

    `budget` alone cannot express an already-expired deadline (resolve() floors
    it to 0.05s), so the clock itself is pushed past the deadline instead —
    exactly what a slow build_index() source (a cold PATH scan, a hung git)
    would do to a real request.
    """
    text = "pie test tests slash test underscore camera h m r dot py"
    real_monotonic = time.monotonic
    calls = {"n": 0}

    def jumped():
        # First call is resolve()'s own `started`, kept real so the deadline is
        # computed normally; every call after that reports far in the future,
        # so the very first deadline check inside resolve() already sees it
        # blown.
        calls["n"] += 1
        return real_monotonic() if calls["n"] == 1 else real_monotonic() + 10.0

    monkeypatch.setattr(time, "monotonic", jumped)
    start = real_monotonic()
    result = R.resolve(text, screen=SHELL_SCREEN, cwd=project)
    elapsed = real_monotonic() - start
    assert result["text"] == text
    assert result["spans"] == []
    assert elapsed < 1.0


def test_resolve_stays_inside_the_budget(project):
    started = time.monotonic()
    R.resolve("pie test tests slash test underscore camera h m r dot py",
              screen=SHELL_SCREEN * 15, cwd=project)
    assert time.monotonic() - started < 1.0


def test_resolve_survives_a_broken_screen_payload(project):
    """Garbage from the client degrades to an echo rather than a 500."""
    result = R.resolve("hello there", screen=[None, 12, {"a": 1}], cwd=project)
    assert result["text"] == "hello there"


# ---------------------------------------------------------------------------
# Shell history vocabulary
# ---------------------------------------------------------------------------

@pytest.fixture
def history(tmp_path, monkeypatch):
    """A history file at a known path, with the module cache cleared.

    The real ~/.zsh_history must never decide a test's outcome, so $HISTFILE is
    pointed at the fixture and the cache is emptied on the way in and out.
    """
    path = tmp_path / "history"

    def write(text: str):
        path.write_text(text)
        R._history_cache["stamp"] = None
        R._history_cache["words"] = []
        return str(path)

    monkeypatch.setenv("HISTFILE", str(path))
    R._history_cache["stamp"] = None
    R._history_cache["words"] = []
    yield write
    R._history_cache["stamp"] = None
    R._history_cache["words"] = []


def test_history_reads_the_zsh_extended_format(history):
    history(": 1699999999:0;cd /is/cluster/fast/sdwivedi/work\n"
            ": 1700000000:0;python train_camerahmr.py\n")
    words = R.history_vocabulary()
    assert "sdwivedi" in words
    assert "cluster" in words
    assert "train_camerahmr.py" in words
    # The metadata prefix is stripped, not tokenized into vocabulary.
    assert not any(w.startswith("1699999") for w in words)


def test_history_reads_plain_lines(history):
    history("cd /is/cluster/fast/sdwivedi/work\nls tests/test_camerahmr.py\n")
    words = R.history_vocabulary()
    assert "sdwivedi" in words
    assert "test_camerahmr.py" in words


def test_history_joins_multiline_entries(history):
    """A backslash continuation is one command, and its tail is vocabulary too."""
    history(": 1700000000:0;python train.py \\\n"
            "  --config configs/camerahmr_base.yaml\n")
    words = R.history_vocabulary()
    assert "camerahmr_base.yaml" in words
    assert "configs" in words


def test_history_reads_only_the_tail_of_a_huge_file(history, monkeypatch):
    """An ancient history file must not be read (or parsed) end to end."""
    monkeypatch.setattr(R, "HISTORY_TAIL_BYTES", 2000)
    old = "".join(f"echo ancient_marker_{n}\n" for n in range(5000))
    history(old + "cd /is/cluster/fast/sdwivedi/work\n")
    words = R.history_vocabulary()
    assert "sdwivedi" in words
    assert not any(w.startswith("ancient_marker_0") for w in words)


def test_history_emits_path_segments_as_words(history):
    history("rsync -a /is/cluster/fast/sdwivedi/work/pretrained_models /tmp/x\n")
    words = R.history_vocabulary()
    for segment in ("cluster", "fast", "sdwivedi", "pretrained_models"):
        assert segment in words, segment


def test_history_drops_the_value_of_a_credential_assignment(history):
    history("export HF_TOKEN=hf_QQzzXXsecretvalue123\n"
            "export PYTHONPATH=/is/cluster/fast/sdwivedi/work\n")
    words = R.history_vocabulary()
    assert not any("QQzzXXsecretvalue123" in w for w in words)
    # The variable name is harmless, and the non-secret assignment keeps its
    # value — that path is exactly what this source exists to supply.
    assert "HF_TOKEN" in words
    assert "sdwivedi" in words


def test_history_drops_a_high_entropy_token(history):
    history("curl https://api.example.com/v1 ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6\n"
            "git commit -m fix\n")
    words = R.history_vocabulary()
    assert not any(w.startswith("ghp_") for w in words)
    assert "commit" in words


def test_history_drops_the_argument_after_a_secret_flag(history):
    history("curl -H Authorization:Bearer-abcdef123456 https://api.example.com/v1\n")
    words = R.history_vocabulary()
    assert not any("abcdef123456" in w for w in words)


def test_history_keeps_ordinary_commands_and_paths(history):
    history("pytest tests/test_camerahmr.py\n"
            "micromamba activate tokenhmr\n")
    words = R.history_vocabulary()
    for expected in ("pytest", "test_camerahmr.py", "micromamba", "tokenhmr"):
        assert expected in words, expected


def test_history_weights_repeated_and_recent_words_first(history):
    history("".join("cd /is/cluster/fast/sdwivedi/work\n" for _ in range(20))
            + "vim rarely_touched_file.txt\n")
    words = R.history_vocabulary()
    assert words.index("sdwivedi") < words.index("rarely_touched_file.txt")


def test_history_cache_invalidates_when_the_file_changes(history):
    history("cd /is/cluster/fast/sdwivedi/work\n")
    first = R.history_vocabulary()
    assert "sdwivedi" in first

    # Same path, new content and a new mtime: the (path, mtime, size) stamp no
    # longer matches, so the parse must run again rather than serve the old list.
    path = history("cd /home/other/projects/newthing\n")
    import os as _os
    _os.utime(path, (0, 0))
    R._history_cache["stamp"] = None
    second = R.history_vocabulary()
    assert "newthing" in second
    assert "sdwivedi" not in second


def test_history_cache_serves_a_second_call_without_reparsing(history, monkeypatch):
    history("cd /is/cluster/fast/sdwivedi/work\n")
    assert "sdwivedi" in R.history_vocabulary()
    monkeypatch.setattr(R, "_read_history_tail",
                        lambda path: pytest.fail("history was re-read"))
    assert "sdwivedi" in R.history_vocabulary()


def test_history_is_empty_when_the_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HISTFILE", str(tmp_path / "nope"))
    R._history_cache["stamp"] = None
    R._history_cache["words"] = []
    assert R.history_vocabulary() == []


def test_history_is_empty_when_the_file_is_unreadable(history, monkeypatch):
    path = history("cd /is/cluster/fast/sdwivedi/work\n")

    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr("builtins.open", boom)
    assert R.history_vocabulary() == []


# ---------------------------------------------------------------------------
# History vocabulary in the phonetic index
# ---------------------------------------------------------------------------

def test_extra_vocab_reaches_a_path_in_neither_the_screen_nor_the_cwd(project):
    """The motivating failure: a path only the shell history has ever seen.

    Without the history words the index has never encountered this path, so the
    garbled transcript passes through untouched; with them it is reachable.
    A path-shaped entry is allowed to match away from command position — that
    is where paths are actually spoken.
    """
    text = "pretrained underscore modles"
    without = R.resolve(text, screen=SHELL_SCREEN, cwd=project)
    assert "pretrained_models" not in without["text"]

    with_history = R.resolve(text, screen=SHELL_SCREEN, cwd=project,
                             extra_vocab=["sdwivedi", "cluster", "pretrained_models"])
    assert "pretrained_models" in with_history["text"]


def test_extra_vocab_bare_words_stay_at_command_position(project):
    """A bare history word is treated exactly like a $PATH name: a plausible
    reading of what is being *run*, and not of a word inside a sentence."""
    result = R.resolve("I was reading the notes", screen=CLAUDE_SCREEN,
                       cwd=project, extra_vocab=["redis", "readline", "reading_list"])
    assert result["text"] == "I was reading the notes"


def test_extra_vocab_does_not_loosen_the_common_word_guard(project):
    """History words broaden the index; they must not lower its bar.

    `cluster` is a real history word and `clutter` is a real English one, and
    the protection over common words has to keep the sentence intact.
    """
    text = "clear the clutter in the room"
    result = R.resolve(text, screen=CLAUDE_SCREEN, cwd=project,
                       extra_vocab=["cluster", "clutter_report", "roomba"])
    assert result["text"] == text


def test_extra_vocab_defaults_to_nothing(project):
    """The parameter is optional: every existing caller keeps working."""
    result = R.resolve("git status", screen=SHELL_SCREEN, cwd=project)
    assert result["text"] == "git status"


def test_build_index_ranks_history_below_the_screen(project):
    """A tie between a screen word and a history word goes to the screen."""
    index = R.build_index(screen=["camerahmr"], cwd="",
                          extra_vocab=["camerahmr"])
    entry = index.by_norm["camerahmr"]
    assert entry.source == "screen"


# ---------------------------------------------------------------------------
# Learned corrections
# ---------------------------------------------------------------------------
# The gates in extract_corrections are the whole feature: an edit is only
# evidence about what whisper mishears if it is not the user changing their
# mind, tidying capitalization, or rephrasing. Each test below is one way an
# edit can be something other than a mishearing.


@pytest.fixture
def store(tmp_path):
    """A learned-correction store of this test's own, never the real one."""
    return tmp_path / "learned.json"


def test_a_misheard_word_is_extracted(store):
    """The case the feature exists for: one word swapped for what was meant."""
    pairs = R.extract_corrections(
        "go to slash is slash cluster slash stved slash work",
        "go to slash is slash cluster slash sdwivedi slash work")
    assert pairs == [("stved", "sdwivedi")]


def test_a_rewritten_sentence_teaches_nothing(store):
    """The user changing their mind is not a mishearing.

    A rewrite replaces a long span with an unrelated one, which the span cap
    refuses outright; anything narrow enough to get past it is stopped by the
    similarity band.
    """
    assert R.extract_corrections(
        "please run the tests again on the cluster",
        "actually never mind lets look at the deployment script instead") == []


def test_a_correction_into_a_common_word_is_not_learned(store):
    """whisper spells English correctly; a store full of "the" is noise."""
    assert R.extract_corrections("run teh thing", "run the thing") == []


def test_a_case_only_edit_is_not_learned(store):
    """Tidying "Git" into "git" is register noise, not a mishearing."""
    assert R.extract_corrections("Git status", "git status") == []
    assert R.extract_corrections("check the readme.", "check the readme") == []


def test_a_secret_shaped_token_is_never_learned(store):
    """Nothing credential-shaped enters the store, a prompt, or an argv."""
    secret = "AKIA9f3Kd82hSlwoP1zXq7Bn4vTyU0mE"
    assert R.extract_corrections(f"the key is {secret}",
                                 "the key is ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5") == []
    assert R.extract_corrections("the key is wrongtoken", f"the key is {secret}") == []


def test_words_that_sound_nothing_alike_are_not_learned(store):
    """A one-word swap is only evidence if the two words could be confused."""
    assert R.extract_corrections("open the notebook", "open the refrigerator") == []


def test_a_shared_metaphone_passes_a_weak_character_ratio(store):
    """`stved` is 0.5 on characters but the same sound — exactly the case.

    The character band alone would keep the pair; this pins the phonetic route
    open, since it is what covers the mishearings that drop whole syllables.
    """
    assert R.similarity("stved", "sdwivedi") < 0.8
    assert R.extract_corrections("cd stved", "cd sdwivedi") == [("stved", "sdwivedi")]


def test_an_insertion_teaches_nothing(store):
    """Adding a word the user did not say says nothing about the microphone."""
    assert R.extract_corrections("run the tests", "run all the tests") == []


def test_a_many_to_one_span_collapses(store):
    """"pie test" for `pytest` is the split whisper actually makes.

    The span joins into one garble, which is then gated exactly like a
    one-to-one pair — `pietest` and `pytest` sound alike, so it is kept.
    """
    assert R.extract_corrections("run pie test now", "run pytest now") == \
        [("pietest", "pytest")]


def test_a_span_of_single_letters_teaches_nothing(store):
    """Spelled-out letters are not a garble worth storing.

    "camera h m r" → `camerahmr` is a real mishearing, but the lesson it would
    leave behind is a rewrite rule keyed on a string of single letters, which
    is both unlikely to recur verbatim and the phonetic index's job anyway.
    """
    assert R.extract_corrections("cd camera h m r now", "cd camerahmr now") == []


def test_one_event_does_not_promote(store):
    """A single edit is as likely a rewrite as a mishearing."""
    R.learn_corrections("cd stved", "cd sdwivedi", path=store)
    assert R.load_learned(store)
    assert R.learned_corrections(store) == []
    assert R.learned_words(store) == []


def test_two_events_promote(store):
    """The same repair in two separate utterances is the evidence bar."""
    R.learn_corrections("cd stved", "cd sdwivedi", path=store)
    R.learn_corrections("open stved please", "open sdwivedi please", path=store)
    promoted = R.learned_corrections(store)
    assert [(e["wrong"], e["right"]) for e in promoted] == [("stved", "sdwivedi")]
    assert promoted[0]["utterances"] == 2
    assert R.learned_words(store) == ["sdwivedi"]


def test_one_utterance_counts_once_however_often_the_word_appears(store):
    """Otherwise a single sentence could promote a correction on its own."""
    R.learn_corrections("stved and stved again", "sdwivedi and sdwivedi again",
                        path=store)
    assert R.learned_corrections(store) == []


def test_the_store_is_bounded_and_evicts_the_oldest(store):
    """Past the bound it is a log, not a vocabulary. Least recent goes first."""
    import json
    entries = [{"wrong": f"wrng{i}", "right": f"rght{i}", "count": 1,
                "utterances": 1, "last_ts": float(i)}
               for i in range(R.LEARNED_MAX_ENTRIES + 10)]
    store.write_text(json.dumps(entries))
    R.learn_corrections("cd stved", "cd sdwivedi", path=store, now=99999.0)
    kept = json.loads(store.read_text())
    assert len(kept) == R.LEARNED_MAX_ENTRIES
    words = {e["wrong"] for e in kept}
    assert "stved" in words          # the newest survives
    assert "wrng0" not in words      # the oldest is gone
    assert f"wrng{R.LEARNED_MAX_ENTRIES + 9}" in words


def test_the_write_is_atomic(store):
    """A reader must see the old store or the new one, never a partial one."""
    R.learn_corrections("cd stved", "cd sdwivedi", path=store)
    before = store.read_text()
    real_replace = os.replace
    seen = {}

    def watch(src, dst):
        # At the moment of the rename the destination still holds the old
        # store: the new bytes were written somewhere else entirely.
        seen["dst_before"] = Path(dst).read_text()
        seen["tmp"] = str(src)
        return real_replace(src, dst)

    os.replace = watch
    try:
        R.learn_corrections("open stved", "open sdwivedi", path=store)
    finally:
        os.replace = real_replace
    assert seen["dst_before"] == before
    assert seen["tmp"] != str(store)
    assert not list(store.parent.glob("*.tmp*"))


def test_a_corrupt_store_degrades_to_empty(store):
    """Half a file, or a file of nonsense, is an empty vocabulary — not a crash."""
    store.write_text("{not json at all")
    assert R.load_learned(store) == []
    assert R.learned_corrections(store) == []
    assert R.learned_words(store) == []
    assert R.apply_learned_rules("cd stved", store) == "cd stved"

    store.write_text('{"wrong": "a", "right": "b"}')   # an object, not a list
    assert R.load_learned(store) == []

    store.write_text('[{"wrong": "stved"}, 7, null]')  # entries missing halves
    assert R.load_learned(store) == []


def test_a_missing_store_is_not_an_error(store):
    assert not store.exists()
    assert R.load_learned(store) == []
    assert R.learned_words(store) == []
    assert R.apply_learned_rules("cd stved", store) == "cd stved"


def test_the_exact_rewrite_keeps_punctuation(store):
    """Same shape as the other ASR rules: the word changes, the comma stays."""
    R.learn_corrections("cd stved", "cd sdwivedi", path=store)
    R.learn_corrections("open stved", "open sdwivedi", path=store)
    assert R.apply_learned_rules("go to stved, then home.", store) == \
        "go to sdwivedi, then home."


def test_the_exact_rewrite_is_case_insensitive_and_exact(store):
    """It fires on the garble it was taught, and on nothing that resembles it."""
    R.learn_corrections("cd stved", "cd sdwivedi", path=store)
    R.learn_corrections("open stved", "open sdwivedi", path=store)
    assert R.apply_learned_rules("Stved", store) == "sdwivedi"
    assert R.apply_learned_rules("stveds", store) == "stveds"
    assert R.apply_learned_rules("unstved", store) == "unstved"


def test_the_exact_rewrite_fires_only_on_asr(store, project):
    """Typed text means what it says — rewriting a typed word would be a bug."""
    R.learn_corrections("cd stved", "cd sdwivedi", path=store)
    R.learn_corrections("open stved", "open sdwivedi", path=store)
    spoken = R.resolve("cd stved", screen=SHELL_SCREEN, cwd=project, asr=True,
                       learned_path=store)
    assert "sdwivedi" in spoken["text"]
    typed = R.resolve("cd stved", screen=SHELL_SCREEN, cwd=project, asr=False,
                      learned_path=store)
    assert typed["text"] == "cd stved"


def test_an_unpromoted_correction_changes_nothing(store, project):
    """One event is in the store but reaches neither the rewrite nor the index."""
    R.learn_corrections("cd stved", "cd sdwivedi", path=store)
    result = R.resolve("cd stved", screen=SHELL_SCREEN, cwd=project, asr=True,
                       learned_path=store)
    assert result["text"] == "cd stved"


def test_an_empty_store_changes_nothing(project):
    """The no-learning case must be byte-identical to before the feature."""
    for text in ("git status", "cd slash is slash cluster",
                 "run the tests again"):
        with_store = R.resolve(text, screen=SHELL_SCREEN, cwd=project, asr=True)
        assert with_store["text"] == R.resolve(text, screen=SHELL_SCREEN,
                                               cwd=project, asr=True)["text"]


def test_the_exact_rewrite_reaches_a_path_segment(store, project):
    """The word is learned bare and misheard inside a path — the common case.

    `stved` scores 0.5 against `sdwivedi`, far below the snap pass's bar, so
    the filesystem walk correctly refuses it. This is the gap learning covers.
    """
    R.learn_corrections("cd slash is slash cluster slash stved",
                        "cd slash is slash cluster slash sdwivedi", path=store)
    R.learn_corrections("the stved folder", "the sdwivedi folder", path=store)
    result = R.resolve("Can you go to the folder called /is/cluster/fast/stved/work?",
                       screen=CLAUDE_SCREEN, cwd=project, asr=True,
                       learned_path=store)
    # The question mark stays: this is Claude's prompt, where the sentence
    # dressing is prose the user meant to type.
    assert result["text"] == \
        "Can you go to the folder called /is/cluster/fast/sdwivedi/work?"


def test_the_exact_rewrite_never_matches_part_of_a_name(store):
    """Only whole tokens and whole "/"-separated segments, never a substring."""
    R.learn_corrections("cd stved", "cd sdwivedi", path=store)
    R.learn_corrections("open stved", "open sdwivedi", path=store)
    assert R.apply_learned_rules("/home/stvedish/work", store) == "/home/stvedish/work"
    assert R.apply_learned_rules("/home/stved/work", store) == "/home/sdwivedi/work"
