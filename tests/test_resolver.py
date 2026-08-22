"""Tests for the dictation resolver.

The suite is weighted the way the feature is: a handful of cases prove the
corrections work, and a much larger set proves that everything else is left
alone. A regression that stops fixing "pie test" is an annoyance; one that
starts rewriting a sentence is the failure this module is built to avoid.
"""

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


def test_rules_are_reduced_outside_shell():
    """Standalone symbols and bare digits are shell-only; joiners survive."""
    assert R.apply_rules("cat log pipe grep error", "claude") == \
        "cat log pipe grep error"
    assert R.apply_rules("tests slash test dot py", "claude") == "tests/test.py"


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
