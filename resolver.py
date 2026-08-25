#!/usr/bin/env python3
"""Code-aware dictation resolver — turns spoken technical text into what was meant.

Speech recognition renders `pytest` as "pie test" and `tests/test_camerahmr.py`
as "tests slash test underscore camera h m r dot py". This module repairs that
locally and deterministically: no cloud call, no LLM. It works from what the
terminal already knows — the words on screen, the filenames under the pane's
cwd, git branches, $PATH executables, tmux names — plus a table of spoken
punctuation rules.

It is a library, not a route: the transcribe path in app.py post-processes a
whisper transcript through it.

The bias is conservative to the point of stubbornness. Unchanged text is the
success case; a confident wrong "fix" is the only real failure, because the user
must then notice and undo it. Every threshold, the common-English-word guard and
the per-register tightening exist to buy that stubbornness.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The whole request must fit in this; the caller re-checks between phases and
# gives up with the input unchanged rather than answering slowly. The transcribe
# path passes a more generous budget — the audio pass dominates its latency
# anyway, so there is nothing to be gained by rushing the snap.
TIME_BUDGET_S = 0.40

# Index caps. A monorepo cwd or a fat $PATH must not turn one dictated line into
# a filesystem walk the user waits on.
MAX_WALK_ENTRIES = 2000
MAX_WALK_DEPTH = 3
MAX_PATH_ENTRIES = 4000
MAX_SCREEN_LINES = 60
# Recently-touched filenames are a short, high-value list, not a listing: past
# the few dozen most recent the names are no longer "what the user is working
# on", which is the only thing this source claims to know.
MAX_GIT_TOUCHED_FILES = 40
SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", ".git", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".tox", ".idea", "target",
})

CWD_CACHE_TTL_S = 5.0
PATH_CACHE_TTL_S = 600.0

# ---------------------------------------------------------------------------
# Common English words
# ---------------------------------------------------------------------------
# Any window that is a plain English word is protected: outside the shell it may
# only be replaced on an exact or near-exact (0.95) match, never on a phonetic
# guess. This is what keeps "run the test again" from becoming a filename.
# The list is deliberately weighted towards the words dictation actually
# produces in prose, not towards coverage of the dictionary.
COMMON_WORDS = frozenset("""
a able about above accept across act action add after again against age ago
agree ahead all allow almost alone along already also although always am among
amount an and animal another answer any anyone anything appear apply are area
argue arm around arrive art as ask at attack attempt available away back bad
bag ball bank base be beat beautiful because become bed been before begin
behind being believe below best better between beyond big bill bit black block
blood blue board boat body book born both bottom box boy break bring broad
brother build building business but buy by call came can cannot car card care
carry case catch cause cell center central century certain chance change
character charge check child choice choose church city civil claim class clear
close coach cold collection college color come common community company
compare computer concern condition conference consider continue control cost
could country county couple course court cover create crime cultural culture
cup current cut dark data daughter day dead deal death decade decide decision
deep degree democratic describe design despite detail determine develop
development die difference different difficult dinner direction director
discover discuss discussion disease do doctor dog door down draw dream drive
drop drug during each early east easy eat economic economy edge education
effect effort eight either election else employee end energy enjoy enough
enter entire environment especially establish even evening event ever every
everybody everyone everything evidence exactly example executive exist expect
experience expert explain eye face fact factor fail fall family far fast
father fear federal feel feeling few field fight figure fill film final
finally financial find fine finger finish fire firm first fish five floor fly
focus follow food foot for force foreign forget form former forward four free
friend from front full fund future game garden gas general generation get girl
give glass go goal good government great green ground group grow growth guess
gun guy hair half hand hang happen happy hard have he head health hear heart
heat heavy help her here herself high him himself his history hit hold home
hope hospital hot hotel hour house how however huge human hundred husband i
idea identify if image imagine impact important improve in include including
increase indeed indicate individual industry information inside instead
institution interest interesting international interview into investment
involve is issue it item its itself job join just keep key kid kill kind
kitchen knowledge know known land language large last late later laugh law
lawyer lay lead leader learn least leave left leg legal less let letter level
lie life light like likely line list listen little live local long look lose
loss lot love low machine magazine main maintain major make man manage
management manager many market marriage material matter may maybe me mean
measure media medical meet meeting member memory mention message method middle
might military million mind minute miss mission model modern moment money
month more morning most mother mouth move movement movie much music must my
myself name nation national natural nature near nearly necessary need network
never new news newspaper next nice night no none nor north not note nothing
notice now number occur of off offer office officer official often oh oil ok
old on once one only onto open operation opportunity option or order
organization other others our out outside over own owner page pain painting
paper parent part participant particular particularly partner party pass past
patient pattern pay peace people per perform performance perhaps period person
personal phone physical pick picture piece place plan plant play player please
point police policy political politics poor popular population position
positive possible power practice prepare present president pressure pretty
prevent price private probably problem process produce product production
professional professor program project property protect prove provide public
pull purpose push put quality question quickly quite race radio raise range
rate rather reach read ready real reality realize really reason receive recent
recently recognize record red reduce reflect region relate relationship
religious remain remember remove report represent republican require research
resource respond response responsibility rest result return reveal rich right
rise risk road rock role room rule run safe same save say scene school science
score sea season seat second section security see seek seem sell send senior
sense series serious serve service set seven several sex shake share she shoot
short shot should shoulder show side sign significant similar simple simply
since sing single sister sit site situation six size skill skin small smile so
social society some somebody someone something sometimes son song soon sort
sound source south southern space speak special specific speech spend sport
spring staff stage stand standard star start state statement station stay step
still stock stop store story strategy street strong structure student study
stuff style subject success successful such suddenly suffer suggest summer
support sure surface system table take talk task tax teach teacher team
technology television tell ten tend term test than thank that the their them
themselves then theory there these they thing think third this those though
thought thousand threat three through throughout throw thus time to today
together tonight too top total tough toward town trade traditional training
travel treat treatment tree trial trip trouble true truth try turn tv two type
under understand unit until up upon us use usually value various very victim
view violence visit voice vote wait walk wall want war watch water way we
weapon wear week weight well west western what whatever when where whether
which while white who whole whom whose why wide wife will win wind window wish
with within without woman wonder word work worker world worry would write
writer wrong yard yeah year yes yet you young your yourself
""".split())


# ---------------------------------------------------------------------------
# Phonetics
# ---------------------------------------------------------------------------
# A simplified double-metaphone: enough of the real algorithm's rules to make
# "pie test"/"pytest" and "num pie"/"numpy" collide, without its 800-line table
# of European name exceptions, which nothing here dictates.

_VOWELS = "AEIOUY"


def metaphone(word: str) -> str:
    """A phonetic key for `word`: same key means "sounds the same".

    Letters that never carry sound in English spelling are dropped, letters that
    share a sound are folded onto one code (c/k/q → K, s/z → S, f/ph/v → F), and
    every vowel that is not word-initial disappears — dictation confuses vowels
    far more often than consonants, so keying on the consonant skeleton is what
    makes the match robust.
    """
    text = re.sub(r"[^A-Z]", "", str(word).upper())
    if not text:
        return ""

    n = len(text)
    out = []

    def at(i: int) -> str:
        return text[i] if 0 <= i < n else ""

    def is_vowel(i: int) -> bool:
        return at(i) in _VOWELS

    # Silent word-initial pairs: gn/kn/pn/wr/ps all speak only their second
    # letter, which is exactly why "knee" and "nee" must key alike.
    i = 0
    if text[:2] in ("GN", "KN", "PN", "WR", "PS", "AE"):
        i = 1
    elif text[:1] == "X":
        out.append("S")
        i = 1
    elif text[:2] == "WH":
        out.append("W")
        i = 2

    # A word-initial vowel is kept (it is audible and distinguishes "act" from
    # "cat"); interior vowels are dropped below.
    if i < n and is_vowel(i):
        out.append("A")
        i += 1

    while i < n:
        c = at(i)
        nxt = at(i + 1)

        if c == at(i - 1) and c != "C":
            # Doubled letters sound once ("running" → RNNK would be wrong).
            i += 1
            continue

        if c in _VOWELS:
            i += 1
            continue

        if c == "B":
            # Silent only after a final M ("dumb").
            if not (at(i - 1) == "M" and i + 1 == n):
                out.append("P")
            i += 1
        elif c == "C":
            if nxt == "H":
                out.append("X")  # "church", "chair"
                i += 2
            elif nxt in "IEY":
                out.append("S")  # soft c: "city", "cell"
                i += 2 if nxt == "I" and at(i + 2) == "A" else 1
            else:
                out.append("K")
                i += 1
        elif c == "D":
            if nxt == "G" and at(i + 2) in "IEY":
                out.append("J")  # "edge", "judge"
                i += 3
            else:
                out.append("T")
                i += 1
        elif c == "G":
            if nxt == "H":
                # "gh" is silent after a vowel ("night", "through") and hard at
                # the start of a syllable ("ghost").
                if is_vowel(i - 1):
                    i += 2
                else:
                    out.append("K")
                    i += 2
            elif nxt == "N":
                i += 2  # "sign", "align"
            elif nxt in "IEY":
                out.append("J")  # soft g: "germ", "gym"
                i += 1
            else:
                out.append("K")
                i += 1
        elif c == "H":
            # Kept wherever it survived the digraph rules above. Real metaphone
            # drops a post-vowel H as silent, but the words this resolver sees
            # are spelled-out acronyms as often as English — dropping the H in
            # `camerahmr` would stop it ever matching the spoken "camera h m r".
            out.append("H")
            i += 1
        elif c == "J":
            out.append("J")
            i += 1
        elif c in "KQ":
            out.append("K")
            i += 1
        elif c == "L":
            out.append("L")
            i += 1
        elif c == "M":
            out.append("M")
            i += 1
        elif c == "N":
            out.append("N")
            i += 1
        elif c == "P":
            if nxt == "H":
                out.append("F")  # "phone", "graph"
                i += 2
            else:
                out.append("P")
                i += 1
        elif c == "R":
            out.append("R")
            i += 1
        elif c == "S":
            if nxt == "H":
                out.append("X")  # "shell"
                i += 2
            elif nxt == "C" and at(i + 2) == "H":
                out.append("SK")  # "school"
                i += 3
            else:
                out.append("S")
                i += 1
        elif c == "T":
            if nxt == "H":
                out.append("0")  # theta; "this", "path"
                i += 2
            elif nxt == "I" and at(i + 2) in "AO":
                out.append("X")  # "-tion", "-tial"
                i += 3
            else:
                out.append("T")
                i += 1
        elif c == "V":
            out.append("F")
            i += 1
        elif c == "W":
            # Audible only before a vowel; "law", "how" end on the vowel sound.
            if is_vowel(i + 1):
                out.append("W")
            i += 1
        elif c == "X":
            out.append("KS")
            i += 1
        elif c == "Z":
            out.append("S")
            i += 1
        else:
            i += 1

    return "".join(out)


# ---------------------------------------------------------------------------
# String similarity
# ---------------------------------------------------------------------------

def damerau_levenshtein(a: str, b: str) -> int:
    """Edit distance counting a transposition as one edit, not two.

    Transposition matters here because dictation and thumb-typing both produce
    swapped adjacent characters far more often than a random substitution.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + cost)
        prev2, prev = prev, cur
    return prev[len(b)]


def similarity(a: str, b: str) -> float:
    """Edit distance normalized to 0..1, where 1.0 is identity."""
    if not a and not b:
        return 1.0
    longest = max(len(a), len(b))
    if longest == 0:
        return 1.0
    return 1.0 - damerau_levenshtein(a, b) / longest


# ---------------------------------------------------------------------------
# Vocabulary index
# ---------------------------------------------------------------------------

# Priority decides ties: what is on screen right now beats what merely exists on
# the filesystem, and both beat the long tail of $PATH. Shell history sits at
# the bottom — it is the broadest source and the least current, so it wins a tie
# only when nothing nearer to the user has an answer at all.
SOURCE_PRIORITY = {
    "screen": 5,
    "cwd": 4,
    "branch": 3,
    "path": 2,
    "tmux": 1,
    "history": 1,
}

# Sources that are broad rather than nearby: hundreds or thousands of names the
# user is not looking at, where something will sound like almost any English
# word. Both are useful for what the user is *running* or has run, and both need
# the extra guards in match_window before a fuzzy hit on them may stand.
_WIDE_SOURCES = frozenset({"path", "history"})

# Carries a separator, so it is a path or a dotted filename rather than a bare
# word — the shape that is plausible mid-sentence and not only after a prompt.
_PATH_SHAPED_RE = re.compile(r"[/._-]")

_SPLIT_RE = re.compile(r"[^A-Za-z0-9_./~=+-]+")
_SUBWORD_RE = re.compile(r"[_\-./]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# What counts as a word from a screen line: long enough to be worth matching,
# and containing a letter so bare numbers and hashes stay out of the index.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-/]+")


def subwords(surface: str) -> list[str]:
    """Split an identifier the way it was probably spoken.

    `test_camerahmr.py` is dictated as three separate chunks, so the index has
    to hold those chunks as well as the whole string, or nothing will match the
    middle of a filename.
    """
    parts: list[str] = []
    for chunk in _SUBWORD_RE.split(surface):
        if not chunk:
            continue
        for piece in _CAMEL_RE.split(chunk):
            if len(piece) >= 2:
                parts.append(piece)
    return parts


def normalize(text: str) -> str:
    """Casefolded, separator-free form — what "the same identifier" means here."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


class Entry:
    """One thing the user might have meant, with everything needed to match it."""

    __slots__ = ("surface", "source", "norm", "key", "word_key")

    def __init__(self, surface: str, source: str) -> None:
        self.surface = surface
        self.source = source
        self.norm = normalize(surface)
        self.key = metaphone(self.norm)
        # Per-subword keys concatenated: the path by which "camera h m r"
        # reaches `camerahmr`. Both the glued form (self.key) and this
        # per-word concatenation are needed — gluing first would make "hmr"
        # one unpronounceable blob, so callers compare against both.
        self.word_key = "".join(metaphone(w) for w in subwords(surface)) or self.key


class Index:
    """The vocabulary for one resolve call, keyed for fast lookup."""

    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self.by_norm: dict[str, Entry] = {}
        self.by_key: dict[str, list[Entry]] = {}
        self._surfaces: set[str] = set()
        self._seen: set[tuple[str, str]] = set()

    def add(self, surface: str, source: str) -> None:
        surface = str(surface).strip()
        if not (2 <= len(surface) <= 120):
            return
        if not any(c.isalpha() for c in surface):
            return
        pair = (surface, source)
        if pair in self._seen:
            return
        self._seen.add(pair)

        entry = Entry(surface, source)
        if not entry.norm:
            return
        self.entries.append(entry)
        self._surfaces.add(surface.lower())

        current = self.by_norm.get(entry.norm)
        if current is None or self.rank(entry) > self.rank(current):
            self.by_norm[entry.norm] = entry
        for key in {entry.key, entry.word_key}:
            if key:
                self.by_key.setdefault(key, []).append(entry)

    def add_many(self, surfaces, source: str) -> None:
        for surface in surfaces:
            self.add(surface, source)

    @staticmethod
    def rank(entry: Entry) -> int:
        return SOURCE_PRIORITY.get(entry.source, 0)

    def has(self, text: str) -> bool:
        """Is this window already, verbatim, a known identifier?

        Case-insensitive but NOT normalized: normalizing would make the spoken
        "tests slash test underscore camera h m r dot py" compare equal to the
        filename it is trying to become, and this guard would then veto the one
        correction the index exists to make. Only a window that is already
        written as an identifier is left alone.
        """
        return str(text).lower() in self._surfaces


def screen_tokens(lines, limit: int = MAX_SCREEN_LINES) -> list[str]:
    """Identifier-shaped words from the terminal buffer, original casing kept.

    Case is preserved because the screen is the one source that shows how the
    user's own project spells things — `CameraHMR` is not `camerahmr`.

    `limit` is the visible screen by default and must stay that way for the
    index: register detection and match_window are entitled to see exactly what
    the user is looking at and nothing older. Only the transcription prompt,
    which merely biases the decode, passes a wider capture.
    """
    tokens: list[str] = []
    for line in list(lines or [])[-max(1, limit):]:
        for tok in _TOKEN_RE.findall(str(line)):
            tok = tok.strip("./-")
            if len(tok) >= 3 and any(c.isalpha() for c in tok):
                tokens.append(tok)
    return tokens


def walk_names(cwd: str, deadline: float = 0.0) -> list[str]:
    """Relative paths and basenames under `cwd`, bounded hard.

    Both forms are indexed: the user says either "test underscore camera h m r
    dot py" or the whole "tests slash test underscore ...", and only one of them
    matches a full relative path.

    `deadline` is checked every ~200 entries rather than every entry — cheap
    enough not to matter, but frequent enough that a huge directory cannot run
    the clock out before the check ever fires.
    """
    names: list[str] = []
    if not cwd or not os.path.isdir(cwd):
        return names
    root_depth = cwd.rstrip(os.sep).count(os.sep)
    try:
        for dirpath, dirnames, filenames in os.walk(cwd):
            if deadline and len(names) % 200 == 0 and time.monotonic() > deadline:
                break
            depth = dirpath.rstrip(os.sep).count(os.sep) - root_depth
            if depth >= MAX_WALK_DEPTH:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames
                               if not d.startswith(".") and d not in SKIP_DIRS]
            for name in list(dirnames) + filenames:
                if name.startswith("."):
                    continue
                names.append(name)
                rel = os.path.relpath(os.path.join(dirpath, name), cwd)
                if rel != name:
                    names.append(rel)
                if len(names) >= MAX_WALK_ENTRIES:
                    return names
    except OSError:
        return names
    return names


def git_branches(cwd: str, deadline: float = 0.0) -> list[str]:
    """Local branch names, or nothing at all — git being absent is normal.

    The subprocess timeout is capped to whatever remains of the request budget,
    never a constant that could alone exceed it — a `timeout=0.5` here would
    outlast the entire TIME_BUDGET_S on its own.
    """
    if not cwd or not os.path.isdir(cwd):
        return []
    timeout = 0.5
    if deadline:
        timeout = min(0.25, max(0.05, deadline - time.monotonic()))
    try:
        p = subprocess.run(
            ["git", "branch", "--format=%(refname:short)"],
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    return [line.strip() for line in p.stdout.splitlines() if line.strip()][:400]


def _git_lines(cwd: str, args: list[str], deadline: float) -> list[str]:
    """One git invocation's stdout lines, or nothing at all.

    Same convention as git_branches, and for the same reasons: the timeout is
    capped to what remains of the request budget, and git being absent or `cwd`
    not being a repo is an ordinary outcome rather than an error to report.
    """
    timeout = 0.5
    if deadline:
        timeout = min(0.25, max(0.05, deadline - time.monotonic()))
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    return p.stdout.splitlines()


def git_touched_files(cwd: str, deadline: float = 0.0) -> list[str]:
    """Basenames of the files this repo was recently working on, newest first.

    The cwd walk already supplies every tracked path, so this source is not
    about reach but about order: the handful of files edited today are what the
    user is talking about, and putting their names in the vocabulary is what
    gets a dictated `resolver.py` back from a monorepo's thousands of names.

    Basenames only. The walk already holds the full relative paths, and it is
    the spoken filename — not the path git prints — that has to be matched.

    Status first because it is "right now"; the log's own order is then kept
    verbatim, since git already emits it newest-commit-first and re-sorting it
    would throw away the one thing this source knows.
    """
    if not cwd or not os.path.isdir(cwd):
        return []
    names: list[str] = []
    seen: set[str] = set()

    def keep(path: str) -> None:
        # A rename prints "old -> new"; the new name is the one that exists.
        # Paths with spaces or quotes in them come back quoted.
        path = path.split(" -> ")[-1].strip().strip('"')
        name = os.path.basename(path.rstrip("/"))
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    for line in _git_lines(cwd, ["status", "--porcelain"], deadline):
        if not line.strip():
            continue
        # Porcelain v1 is fixed-width: two status columns and a space, then the
        # path. Slicing beats splitting, since the path may itself hold spaces.
        keep(line[3:])
        if len(names) >= MAX_GIT_TOUCHED_FILES:
            return names
    # `--pretty=format:` prints an empty line per commit, which the strip below
    # drops; --name-only then gives one path per line with no status columns.
    for line in _git_lines(cwd, ["log", "--name-only", "--pretty=format:", "-5"],
                           deadline):
        if not line.strip():
            continue
        keep(line.strip())
        if len(names) >= MAX_GIT_TOUCHED_FILES:
            break
    return names


# Unlocked by design: every access is a single dict get/set, atomic under the
# GIL, and a torn read at worst serves one stale or one-scan-early result —
# never a corrupt one. The one place two steps must not interleave (_cwd_cache's
# check-then-clear below) has its own lock.
_path_cache: dict[str, object] = {"at": 0.0, "names": []}


def path_commands(deadline: float = 0.0) -> list[str]:
    """Executable basenames on $PATH, cached process-wide — $PATH rarely moves.

    Only a cold cache does any work, so the deadline check happens once, before
    that scan starts: a hot cache is a dict lookup and never worth skipping.
    """
    now = time.monotonic()
    if _path_cache["names"] and now - float(_path_cache["at"]) < PATH_CACHE_TTL_S:
        return list(_path_cache["names"])  # type: ignore[arg-type]
    if deadline and now > deadline:
        return list(_path_cache["names"])  # type: ignore[arg-type]

    names: list[str] = []
    seen: set[str] = set()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    name = entry.name
                    if name.startswith(".") or name in seen:
                        continue
                    seen.add(name)
                    names.append(name)
                    if len(names) >= MAX_PATH_ENTRIES:
                        break
        except OSError:
            continue
        if len(names) >= MAX_PATH_ENTRIES:
            break
    _path_cache["at"] = now
    _path_cache["names"] = names
    return list(names)


_cwd_cache: dict[str, tuple[float, list[str], list[str]]] = {}
_cwd_cache_lock = threading.Lock()


def cwd_vocabulary(cwd: str, deadline: float = 0.0) -> tuple[list[str], list[str]]:
    """(filenames, branches) for `cwd`, cached briefly.

    Five seconds is long enough that a burst of keystroke-debounced resolves
    walks the tree once, and short enough that a file created mid-session shows
    up while the user is still talking about it.

    Recently-touched filenames join the names rather than becoming a third
    source: they are cwd files, indexed at the same "cwd" priority, and the
    only thing that distinguishes them is that they come first — which is
    exactly what a list already carries.
    """
    now = time.monotonic()
    hit = _cwd_cache.get(cwd)
    if hit and now - hit[0] < CWD_CACHE_TTL_S:
        return hit[1], hit[2]
    if deadline and now > deadline:
        return [], []
    names = walk_names(cwd, deadline)
    branches = [] if (deadline and time.monotonic() > deadline) else git_branches(cwd, deadline)
    if not (deadline and time.monotonic() > deadline):
        names = git_touched_files(cwd, deadline) + names
    # Unbounded growth is not a risk worth code, but a long-running server
    # visiting many cwds should not keep every one forever. The check and the
    # clear must not interleave across threads, or a clear from one racing
    # request can drop an entry a second request just added.
    with _cwd_cache_lock:
        if len(_cwd_cache) > 32:
            _cwd_cache.clear()
        _cwd_cache[cwd] = (now, names, branches)
    return names, branches


# ---------------------------------------------------------------------------
# Shell history vocabulary
# ---------------------------------------------------------------------------
# The screen and the cwd only know about the place the user is standing. The
# paths they dictate are often somewhere else entirely — a cluster mount typed a
# hundred times last month and not once today — and for those the history file
# is the only source that has ever seen the words.

HISTORY_TAIL_LINES = 2000
HISTORY_TAIL_BYTES = 400_000
HISTORY_COMMANDS = 800
HISTORY_MAX_WORDS = 800
HISTORY_WORDS_PER_COMMAND = 12

# zsh's EXTENDED_HISTORY prefix: ": <start>:<elapsed>;<command>".
_HIST_META_RE = re.compile(r"^:\s*\d+:\d+;")
# Assignment whose value is a credential by name. Matched on the LHS only, so
# `PYTHONPATH=/is/cluster/...` keeps its value and `API_TOKEN=hunter2` does not.
_SECRET_LHS_RE = re.compile(r"(?i)(pass|token|secret|key|auth|cred)")
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)=(.*)$")
# Flags whose next argument is a credential rather than a word worth learning.
_SECRET_FLAGS = frozenset({
    "-H", "--header", "--password", "--pass", "--token", "--secret",
    "--api-key", "--apikey", "--key", "-u", "--user", "--auth", "-p",
    "--credential", "--credentials", "-d", "--data",
})
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
# Shell syntax is never something the user dictates, so a token still carrying
# any of it after the quote strip is punctuation rather than vocabulary:
# `${#PS1`, `2>/dev/null`, `$CMUX_DIR`, `*.py`, `foo=bar,baz`.
_SHELL_SYNTAX_RE = re.compile(r"[$*?!#%^~@:,+\\\[\]{}()<>=|&;\"'`]")


def _is_secretish(token: str) -> bool:
    """Does this token have the shape of a credential rather than a name?

    Deliberately trigger-happy: a vocabulary word wrongly dropped costs one
    missed correction, while a leaked one is handed to a subprocess as argv
    where any `ps` can read it. Two shapes are exempted because they are what
    this source exists to supply and neither looks like a key: anything with a
    "/" is a path, and a token whose separators split it into ordinary words
    (`rarely_touched_file.txt`, `train_camerahmr_stage2.yaml`) is a filename.
    """
    if len(token) < 20:
        return False
    if _HEX_RE.match(token) and len(token) >= 32:
        return True

    # A "/" alone does not make something a path — an AWS secret key is mostly
    # base64 and carries slashes too — so the test is whether the pieces the
    # separators cut it into read as words. Short pieces that are letters, or
    # letters trailed by digits (`stage2`, `epoch120`), are name-shaped; a
    # 30-character run of mixed case and digits is not.
    pieces = [p for p in _SUBWORD_RE.split(token) if p]

    def name_shaped(piece: str) -> bool:
        if len(piece) > 14:
            return False
        return bool(re.fullmatch(r"[A-Za-z]+[0-9]*|[0-9]+", piece))

    if len(pieces) >= 2 and all(name_shaped(p) for p in pieces):
        return False
    # A URL's scheme is not a name-shaped piece, but the host and repo path
    # after it are exactly the vocabulary wanted (`saidwivedi`, `PocketTUI`).
    body = re.sub(r"^[a-z][a-z0-9+.-]*://", "", token)
    if "/" in token and all(name_shaped(p) for p in re.split(r"[/.]", body) if p):
        return False
    # A long run mixing cases and digits is what a token or a hash looks like;
    # a name that long is almost always separated into words somehow.
    classes = (any(c.islower() for c in token) + any(c.isupper() for c in token)
               + any(c.isdigit() for c in token))
    return classes >= 2


def _history_files() -> list[str]:
    """Candidate history paths, most authoritative first."""
    env = os.environ.get("HISTFILE", "").strip()
    if env:
        return [os.path.expanduser(env)]
    home = os.path.expanduser("~")
    return [os.path.join(home, ".zsh_history"), os.path.join(home, ".bash_history")]


def _read_history_tail(path: str) -> list[str]:
    """The last few thousand lines of `path`, or nothing at all.

    Only the tail is read: a history file that has been accumulating for years
    is megabytes, and the recent end is the only part whose vocabulary is worth
    biasing towards anyway.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > HISTORY_TAIL_BYTES:
                fh.seek(size - HISTORY_TAIL_BYTES)
                fh.readline()  # discard the partial line the seek landed in
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", "replace")
    return text.splitlines()[-HISTORY_TAIL_LINES:]


def _history_commands(lines: list[str]) -> list[str]:
    """Whole commands from raw history lines, both zsh formats and bash.

    zsh writes a "\\" continuation for a multi-line command and repeats no
    metadata prefix on the continuation, so a line is a new command exactly when
    the previous one did not end in a backslash.
    """
    commands: list[str] = []
    continuing = False
    for line in lines:
        stripped = _HIST_META_RE.sub("", line) if _HIST_META_RE.match(line) else line
        if continuing and commands:
            commands[-1] += "\n" + stripped
        else:
            commands.append(stripped)
        continuing = stripped.endswith("\\")
    return [c for c in commands if c.strip()]


def _command_words(command: str) -> list[str]:
    """Vocabulary words from one command line, credentials filtered out.

    Path tokens contribute their segments as well as the whole string: whisper
    needs "sdwivedi" and "cluster" as words it can emit, and the joined
    `/is/cluster/fast/sdwivedi/work` biases nothing on its own.
    """
    words: list[str] = []
    skip_next = False
    for raw in command.split():
        token = raw.strip("'\"`;|&()<>{}[],")
        if skip_next:
            skip_next = False
            continue
        if raw in _SECRET_FLAGS or token in _SECRET_FLAGS:
            skip_next = True
            continue
        if not token:
            continue
        assign = _ASSIGN_RE.match(token)
        if assign:
            name, value = assign.group(1), assign.group(2)
            if _SECRET_LHS_RE.search(name):
                # The name itself is harmless and often worth having; only the
                # value it holds is dropped.
                token = name
            else:
                token = name
                if value and not _is_secretish(value):
                    words.extend(_path_words(value))
        if _is_secretish(token):
            continue
        words.extend(_path_words(token))
    return words


def _path_words(token: str) -> list[str]:
    """The token itself when it is a plain word, plus its path segments."""
    out: list[str] = []
    if _is_secretish(token) or _SHELL_SYNTAX_RE.search(token):
        return out

    def keep(word: str) -> None:
        word = word.strip("./-~")
        if _SHELL_SYNTAX_RE.search(word):
            return
        if len(word) >= 3 and any(c.isalpha() for c in word) and word not in out:
            out.append(word)

    if "/" in token:
        for segment in token.split("/"):
            keep(segment)
        # The whole path is still worth having when it is short enough to be
        # spoken as one thing; a 90-character one only eats prompt budget.
        if len(token) <= 40:
            keep(token)
    else:
        keep(token)
    return out


# (path, mtime, size) -> words. Parsing a history file is far too expensive to
# repeat per keystroke-debounced resolve, and the file only changes when a shell
# writes to it, so the stat triple is an exact staleness test.
_history_cache: dict[str, object] = {"stamp": None, "words": []}


def history_vocabulary(deadline: float = 0.0) -> list[str]:
    """Words from the user's shell history, most worth biasing towards first.

    Ordered by a frequency-and-recency weight: a path typed once last year is
    behind one typed twenty times this week. Missing, unreadable or empty
    history is not an error — it is the common case on a fresh box, and the
    caller simply gets a thinner vocabulary.
    """
    if deadline and time.monotonic() > deadline:
        return list(_history_cache["words"])  # type: ignore[arg-type]

    path = ""
    stamp = None
    for candidate in _history_files():
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        path, stamp = candidate, (candidate, st.st_mtime, st.st_size)
        break
    if not path:
        _history_cache["stamp"] = None
        _history_cache["words"] = []
        return []

    if _history_cache["stamp"] == stamp:
        return list(_history_cache["words"])  # type: ignore[arg-type]

    commands = _history_commands(_read_history_tail(path))[-HISTORY_COMMANDS:]
    # Repetition is the stronger signal and recency the tiebreaker, not the
    # other way round: a word typed in thirty commands is vocabulary the user
    # lives in, while thirty distinct words from one afternoon's throwaway loop
    # (`echo LINE_1` … `echo LINE_60`) are each seen exactly once. Summing a
    # per-occurrence recency would rank those sixty one-offs above a path used
    # daily for a year, so occurrences are counted and recency only breaks ties
    # between words of equal count.
    counts: dict[str, int] = {}
    last_seen: dict[str, int] = {}
    total = len(commands) or 1
    for position, command in enumerate(commands):
        # One command may not flood the vocabulary. A single `printf` loop or a
        # pasted 60-argument invocation produces dozens of distinct
        # identifier-shaped tokens, all with the same weight, and unbounded they
        # crowd out the paths the user actually types every day.
        for word in _command_words(command)[:HISTORY_WORDS_PER_COMMAND]:
            counts[word] = counts.get(word, 0) + 1
            last_seen[word] = position

    def weight(word: str) -> float:
        return counts[word] + last_seen[word] / total

    ranked = sorted(counts, key=lambda w: (-weight(w), w))
    words = ranked[:HISTORY_MAX_WORDS]
    _history_cache["stamp"] = stamp
    _history_cache["words"] = words
    return list(words)


# ---------------------------------------------------------------------------
# SSH config hosts
# ---------------------------------------------------------------------------
# The hosts a user says out loud — "ssh into galton", "scp it to the cluster" —
# are names no other source has: they are not on screen, not under the cwd, and
# a host reached daily for a year may be nowhere in the history tail.

SSH_CONFIG_PATH = "~/.ssh/config"
SSH_MAX_HOSTS = 200

# A pattern is a rule about hosts, not the name of one: `Host *` carries global
# options and `Host *.cluster` matches a family. Neither is ever spoken.
_SSH_GLOB_CHARS = "*?"

# (path, mtime, size) -> hosts, the same exact staleness test _history_cache
# uses. No deadline parameter: a config file is dozens of lines, so there is no
# scan here worth abandoning half-way — unlike the history tail, which is read
# and parsed by the hundred-kilobyte.
_ssh_cache: dict[str, object] = {"stamp": None, "hosts": []}


def ssh_hosts() -> list[str]:
    """Host names and aliases from ~/.ssh/config, in the order they appear.

    Only that one file. `Include` directives are deliberately not followed: the
    included files are usually generated fleet inventories of hundreds of hosts,
    which is a vocabulary this feature would be worse for having.

    A missing, unreadable or hostless config is the common case and not an
    error — the caller simply gets nothing.
    """
    path = os.path.expanduser(SSH_CONFIG_PATH)
    try:
        st = os.stat(path)
    except OSError:
        _ssh_cache["stamp"] = None
        _ssh_cache["hosts"] = []
        return []
    stamp = (path, st.st_mtime, st.st_size)
    if _ssh_cache["stamp"] == stamp:
        return list(_ssh_cache["hosts"])  # type: ignore[arg-type]

    try:
        with open(path, "r", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []

    hosts: list[str] = []
    seen: set[str] = set()
    for line in lines:
        # ssh_config is "keyword value..." on whitespace (or an "=" the keyword
        # may be separated by), with the keyword case-insensitive.
        parts = line.strip().replace("=", " ", 1).split()
        if len(parts) < 2 or parts[0].startswith("#"):
            continue
        keyword = parts[0].lower()
        if keyword not in ("host", "hostname"):
            continue
        # `Host a b c` defines three aliases; HostName normally takes one value,
        # and reading the rest of its line the same way costs nothing.
        for value in parts[1:]:
            if any(c in value for c in _SSH_GLOB_CHARS) or value in seen:
                continue
            seen.add(value)
            hosts.append(value)
            if len(hosts) >= SSH_MAX_HOSTS:
                break
        if len(hosts) >= SSH_MAX_HOSTS:
            break

    _ssh_cache["stamp"] = stamp
    _ssh_cache["hosts"] = hosts
    return list(hosts)


# ---------------------------------------------------------------------------
# Learned corrections
# ---------------------------------------------------------------------------
# When the user edits a transcript in the compose box before sending it, the
# edit is a labelled example: whisper heard one thing, the user meant another.
# Nothing about it is asked for and nothing about it is shown — the pair is
# extracted from the diff, and once the same pair has turned up in two separate
# utterances the corrected word becomes part of this user's vocabulary.
#
# Two utterances, not one, because a single edit is as likely to be the user
# changing their mind as it is to be a mishearing. Requiring the same wrong
# word to be corrected the same way twice is what separates the two: a rewrite
# is never repeated verbatim, a mishearing always is.

LEARNED_PATH = HERE / ".voice_learned.json"

# Past this many pairs the store is a log rather than a vocabulary. Bounded by
# eviction of the least recently reinforced, so the words the user has stopped
# saying make room for the ones they have started saying.
LEARNED_MAX_ENTRIES = 500

# How many separate utterances must show the same correction before it is
# believed. See above: this is the whole gate against learning a rewrite.
LEARNED_PROMOTE_AT = 2

# A replace span wider than this on either side is a rewritten phrase, not a
# misheard word. Three tokens is enough for the compound cases whisper actually
# produces ("camera h m r" for `camerahmr`) and short enough that a re-worded
# sentence has no way through.
LEARNED_MAX_SPAN = 3

# The similarity floor for believing that `right` is what `wrong` sounded like.
# Below it the two words share nothing acoustically and the edit was the user
# saying something else. A shared metaphone key passes independently: `stved`
# and `sdwivedi` are 0.615 on characters but the same sound, which is exactly
# the case learning exists to cover.
LEARNED_MIN_SIMILARITY = 0.40

# (mtime, size) -> entries, the same staleness test _history_cache uses.
_learned_cache: dict[str, object] = {"stamp": None, "entries": []}


def _learnable_word(token: str) -> str:
    """`token` stripped of dictation punctuation, or "" if it is not a word.

    Identifier-shaped means letters, digits and the separators that appear
    inside real names — a token carrying a slash is a path, and paths are the
    snap pass's business, not this one.
    """
    word = token.strip().strip(",.!?;:\"'()[]{}")
    if not (2 <= len(word) <= 64):
        return ""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]*", word):
        return ""
    if not any(c.isalpha() for c in word):
        return ""
    return word


def _learnable_pair(wrong: str, right: str) -> tuple[str, str] | None:
    """One (misheard, corrected) pair, or None if the edit teaches nothing.

    Every gate here is a way for an edit to be something other than a
    mishearing, and each one has cost a wrong lesson somewhere:

    * A pure case or punctuation change is register noise — the user tidying
      "Git" into "git" is already the sentence-dressing rule's job.
    * A `right` that is a bare common English word teaches whisper a word it
      already knows; the store would fill with "the" and "for".
    * Words that sound nothing alike were a rewrite, not a repair.
    * Anything credential-shaped never enters a file, a prompt or an argv.
    """
    wrong, right = _learnable_word(wrong), _learnable_word(right)
    if not wrong or not right:
        return None
    if wrong.lower() == right.lower():
        return None
    if _is_secretish(wrong) or _is_secretish(right):
        return None
    # Whisper spells English correctly; a correction into a bare common word is
    # the user rephrasing. `right` carrying a separator or a digit is a name
    # that merely happens to start with one ("data_v2"), and is kept.
    if right.lower() in COMMON_WORDS and right.isalpha():
        return None
    if similarity(wrong.lower(), right.lower()) < LEARNED_MIN_SIMILARITY \
            and metaphone(wrong) != metaphone(right):
        return None
    return wrong, right


def extract_corrections(heard: str, sent: str) -> list[tuple[str, str]]:
    """The (misheard, corrected) pairs one edited transcript is evidence for.

    `heard` is the text this module handed the compose box; `sent` is what the
    user actually sent after editing it. The difference between them is aligned
    token-wise, and only a `replace` — a span of words swapped for a span of
    words — can carry a mishearing. An insertion is the user adding something
    they did not say, a deletion is them cutting something they did; neither
    says anything about what whisper heard wrong.

    Returns pairs in the order they appear, deduplicated. A rewritten sentence
    returns nothing: its replace spans are far wider than LEARNED_MAX_SPAN, and
    what survives that is caught by the similarity band in _learnable_pair.
    """
    a, b = str(heard or "").split(), str(sent or "").split()
    if not a or not b:
        return []
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    try:
        opcodes = difflib.SequenceMatcher(None, [t.lower() for t in a],
                                          [t.lower() for t in b],
                                          autojunk=False).get_opcodes()
    except Exception:  # noqa: BLE001 — an unlearnable edit, not an error
        return []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag != "replace":
            continue
        left, right = a[i1:i2], b[j1:j2]
        if len(left) > LEARNED_MAX_SPAN or len(right) > LEARNED_MAX_SPAN:
            continue
        # Equal-length spans align positionally: word one was heard as word
        # one. Unequal spans have no such alignment in general, and the one
        # heuristic worth having is the many-to-one case whisper actually
        # produces — "camera h m r" for `camerahmr` — where the whole span
        # collapses into a single word. Anything else is skipped rather than
        # guessed at.
        if len(left) == len(right):
            candidates = list(zip(left, right))
        elif len(right) == 1:
            candidates = [("".join(_learnable_word(t) for t in left), right[0])]
        else:
            continue
        for wrong, corrected in candidates:
            pair = _learnable_pair(wrong, corrected)
            if pair and pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    return pairs


def load_learned(path=None) -> list[dict]:
    """The learned-correction store, or an empty list.

    A missing, unreadable or malformed store is the common case (nothing has
    been learned yet) and never an error: the caller simply gets no learned
    vocabulary. Cached on (mtime, size) like history_vocabulary, so the
    transcribe path re-reads only when learn_corrections has written.
    """
    target = Path(path) if path else LEARNED_PATH
    try:
        st = os.stat(target)
    except OSError:
        if not path:
            _learned_cache["stamp"] = None
            _learned_cache["entries"] = []
        return []
    stamp = (str(target), st.st_mtime, st.st_size)
    if not path and _learned_cache["stamp"] == stamp:
        return list(_learned_cache["entries"])  # type: ignore[arg-type]

    entries: list[dict] = []
    try:
        with open(target, "r", errors="replace") as fh:
            raw = json.load(fh)
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                wrong = str(item.get("wrong", "") or "")
                right = str(item.get("right", "") or "")
                if not wrong or not right:
                    continue
                entries.append({
                    "wrong": wrong,
                    "right": right,
                    "count": int(item.get("count", 0) or 0),
                    "utterances": int(item.get("utterances", 0) or 0),
                    "last_ts": float(item.get("last_ts", 0.0) or 0.0),
                })
    except Exception:  # noqa: BLE001 — a corrupt store is an empty one
        entries = []

    if not path:
        _learned_cache["stamp"] = stamp
        _learned_cache["entries"] = entries
    return list(entries)


def learn_corrections(heard: str, sent: str, path=None, now: float = 0.0) -> int:
    """Fold one edited transcript into the store. Returns the pairs recorded.

    Each pair the edit is evidence for has its `utterances` count raised by
    one — one edit is one utterance's worth of evidence for a given pair, no
    matter how many times the word appeared in it, which is what keeps a single
    sentence from promoting a correction on its own.

    The write is atomic (tmp + rename): the transcribe path reads this file on
    every request, and a reader must see either the old store or the new one,
    never a half-written one.
    """
    pairs = extract_corrections(heard, sent)
    if not pairs:
        return 0
    target = Path(path) if path else LEARNED_PATH
    stamp = now or time.time()

    entries = load_learned(path)
    by_key = {(e["wrong"].lower(), e["right"].lower()): e for e in entries}
    for wrong, right in pairs:
        entry = by_key.get((wrong.lower(), right.lower()))
        if entry is None:
            entry = {"wrong": wrong, "right": right, "count": 0,
                     "utterances": 0, "last_ts": stamp}
            by_key[(wrong.lower(), right.lower())] = entry
            entries.append(entry)
        entry["count"] += 1
        entry["utterances"] += 1
        entry["last_ts"] = stamp

    # Oldest-by-last_ts first out: the bound is on a vocabulary, and the words
    # this user has not said in months are the ones worth losing.
    if len(entries) > LEARNED_MAX_ENTRIES:
        entries.sort(key=lambda e: e["last_ts"], reverse=True)
        entries = entries[:LEARNED_MAX_ENTRIES]

    tmp = target.with_name(target.name + f".tmp{os.getpid()}")
    try:
        with open(tmp, "w") as fh:
            json.dump(entries, fh)
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 0
    if not path:
        _learned_cache["stamp"] = None
    return len(pairs)


def learned_corrections(path=None) -> list[dict]:
    """The promoted entries — those seen in enough separate utterances.

    Most recently reinforced first, which is the order every consumer wants:
    the prompt takes the head of this list, and the rewrite pass reads it whole.
    """
    entries = [e for e in load_learned(path)
               if e.get("utterances", 0) >= LEARNED_PROMOTE_AT]
    entries.sort(key=lambda e: e["last_ts"], reverse=True)
    return entries


def learned_words(path=None) -> list[str]:
    """The corrected spellings this user has taught, most recent first."""
    words: list[str] = []
    seen: set[str] = set()
    for entry in learned_corrections(path):
        word = entry["right"]
        if word.lower() not in seen:
            seen.add(word.lower())
            words.append(word)
    return words


def apply_learned_rules(text: str, path=None) -> str:
    """Rewrite tokens the user has twice corrected by hand. ASR transcripts only.

    An exact, case-insensitive match, with the token's punctuation kept — the
    same shape as the other ASR rules, and deliberately no fuzzier than that.
    The evidence is that this exact garble meant this exact word; it is not
    evidence about anything that merely resembles the garble, and the phonetic
    index is already where resemblance is judged.

    A path segment counts as a match. `sdwivedi` is learned as a bare word,
    because that is how the user corrected it, but the place it is misheard
    most is inside a dictated home path — and the snap pass cannot help there,
    since a mishearing that drops syllables scores below its bar. Only whole
    "/"-separated segments are considered, so a garble is never matched against
    part of some longer name.
    """
    entries = learned_corrections(path)
    if not entries:
        return text
    # Most recent first, so the newest lesson wins if the user has corrected
    # the same garble two different ways.
    table: dict[str, str] = {}
    for entry in entries:
        table.setdefault(entry["wrong"].lower(), entry["right"])
    tokens = text.split()
    out = list(tokens)
    for i, token in enumerate(tokens):
        bare = token.strip(",.!?;:")
        if not bare:
            continue
        head = token[:len(token) - len(token.lstrip(",.!?;:"))]
        tail = token[len(head) + len(bare):]
        replacement = table.get(bare.lower())
        if replacement is None and "/" in bare:
            segments = bare.split("/")
            hit = False
            for j, segment in enumerate(segments):
                mapped = table.get(segment.lower())
                if mapped is not None:
                    segments[j] = mapped
                    hit = True
            replacement = "/".join(segments) if hit else None
        if replacement is None:
            continue
        out[i] = head + replacement + tail
    return " ".join(out)


def build_index(screen=None, cwd: str = "", tmux_names=None,
                deadline: float = 0.0, extra_vocab=None) -> Index:
    """Assemble the vocabulary for one request from every source available.

    `deadline` bounds the expensive sources — the cwd walk, git branches, a
    cold $PATH scan. Screen tokens are cheap and always included; everything
    else is skipped once the deadline has passed, which just means a thinner
    vocabulary rather than a slow answer.

    `extra_vocab` is words the caller already has in hand — shell history, in
    practice. It is added last and at the lowest priority: it broadens what the
    index can reach without letting a word the user typed months ago outrank
    the filename in front of them.
    """
    index = Index()
    # Added in priority order so the first surface to claim a normalized form is
    # the one from the most trustworthy source.
    index.add_many(screen_tokens(screen), "screen")
    if cwd and not (deadline and time.monotonic() > deadline):
        names, branches = cwd_vocabulary(cwd, deadline)
        index.add_many(names, "cwd")
        index.add_many(branches, "branch")
    index.add_many(path_commands(deadline), "path")
    if tmux_names and not (deadline and time.monotonic() > deadline):
        index.add_many(tmux_names, "tmux")
    if extra_vocab:
        index.add_many(extra_vocab, "history")
    return index


# ---------------------------------------------------------------------------
# Register detection
# ---------------------------------------------------------------------------

# A prompt either ends the line (the cursor sits after it) or has the command
# the user already typed following it, so the sigil is matched wherever it falls
# rather than only at end of line.
_PROMPT_RE = re.compile(
    r"(^|\s)[$%#❯›»➜)]\s|(^|\s)[$%#❯›»]\s*$"
    r"|^\S*[\w.-]+@[\w.-]+\S*[:\s].*$"
)
_CLAUDE_MARKERS = (
    "esc to interrupt", "? for shortcuts", "claude code", "bypassing permissions",
    "auto-accept edits", "⏵⏵", "✻ welcome to claude",
)
_EDITOR_MARKERS = ("-- insert --", "-- visual --", "-- normal --", "-- replace --")


def detect_register(screen) -> str:
    """Which program is reading the keyboard: "shell", "claude" or "editor".

    Register decides how bold the resolver may be. Only a shell prompt earns the
    loose threshold, because there a wrong token is one visible word on a
    command line the user reads before pressing enter. Anything unrecognized is
    treated as claude — the conservative default, since text typed into a prompt
    or an editor is prose far more often than it is a command.
    """
    lines = [str(line) for line in (screen or [])][-40:]
    lowered = [line.lower() for line in lines]

    for line in lowered:
        if any(marker in line for marker in _EDITOR_MARKERS):
            return "editor"
    for line in lowered:
        if any(marker in line for marker in _CLAUDE_MARKERS):
            return "claude"

    # A prompt anywhere in the last few lines means a shell: output printed
    # below the last prompt is normal, so requiring the very last line to be one
    # would misread every session that has run a command.
    recent = [line.rstrip() for line in lines if line.strip()][-6:]
    for line in reversed(recent):
        if _PROMPT_RE.search(line):
            return "shell"

    # Claude Code's input box is drawn in box characters and is the last thing on
    # screen; a vim status line is not, so this check comes after the prompt one.
    for line in lines[-6:]:
        if line.count("│") >= 2 or line.startswith("╭") or line.startswith("╰"):
            return "claude"
    return "claude"


# Thresholds by register. The shell is where identifiers live, so it gets the
# loosest bar; everywhere else the user is probably writing sentences.
THRESHOLDS = {"shell": 0.80, "claude": 0.85, "editor": 0.85}

# A common English word may only be replaced by a match this good — i.e. an
# exact or separator-only difference, never a phonetic guess.
PROTECTED_MIN = 0.95


# ---------------------------------------------------------------------------
# Spoken-syntax rules
# ---------------------------------------------------------------------------

# Extensions worth gluing a bare "dot" onto: "dot py" is a filename, "dot" at
# the end of a sentence is a full stop.
EXTENSIONS = frozenset("""
py js ts jsx tsx html css scss sh bash zsh json md txt yaml yml toml ini cfg
csv tsv xml sql rs go java rb php c cc cpp h hpp lock log png jpg svg pdf gz
zip tar env
""".split())

NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20",
}

# Spoken names for characters that join the words on either side into one token.
JOINERS = {
    "underscore": "_",
    "slash": "/",
    "dot": ".",
    "period": ".",
    "colon": ":",
    "equals": "=",
    "tilde": "~",
    "plus": "+",
}
# "at" is deliberately absent: as a joiner it would rewrite "the dot at the end"
# into an email address, and an "@" is rare enough in dictated commands that the
# trade is not close.

# Words that can only be a preposition before a path, never a path segment
# themselves: a "slash" after one of these starts an absolute path rather than
# joining onto the word.
_PATH_LEAD_WORDS = frozenset("to in into from at the a my".split())

# Dictation hears "cd" as an ordinary word about as often as it hears the
# command, so the rewrite is confined to command position (see apply_rules).
CD_HOMOPHONES = frozenset(("cd", "c.d.", "seedy", "cede", "seedee"))

# Commands whose argument is a path, so a "slash" right after one opens an
# absolute path rather than joining onto the command's name. Deliberately short:
# a word not on this list is treated as a path segment, which is the safe miss.
_PATH_COMMANDS = frozenset("""
cd ls ll cat less more tail head rm rmdir mkdir touch cp mv chmod chown du df
find grep vim vi nano code open stat wc realpath ln tree
""".split())

# Spoken names for characters that stand alone with spaces around them.
STANDALONE = {
    "pipe": "|",
    "star": "*",
    "asterisk": "*",
    "ampersand": "&",
    "caret": "^",
}

# Tokens that separate commands rather than name anything. The matcher has to
# skip any window containing one: they carry no letters, so normalize() erases
# them and the window scores as whatever word sits beside it.
OPERATORS = frozenset(("|", "&", "^", ";", "&&", "||", ">", ">>", "<"))

# iOS turns a dictated "dash dash" into an em dash and a lone "dash" into an en
# dash, so the character the phone actually sends has to undo back to the
# hyphens the user said.
_EM_DASHES = ("—", "—")
_EN_DASHES = ("–", "–")

_IDENT_CHARS = re.compile(r"[A-Za-z0-9_./~=+*|&^:@-]")


def _is_wordish(tok: str) -> bool:
    return bool(tok) and bool(re.match(r"^[A-Za-z0-9]", tok))


# A short flag as it is dictated: one or two letters ("dash m", "dash rf"),
# optionally with the flag's argument already glued on by the recognizer
# ("dash n5"). Longer runs are words, and a word right of a dash mid-line is a
# hyphenated name rather than an option.
_FLAG_SHAPED = re.compile(r"^[A-Za-z]{1,2}[0-9]*$")


def _at_flag_position(out: list[str], right: str,
                      command_only: bool = False) -> bool:
    """Is a single dash here starting a flag rather than hyphenating a name?

    Verbatim transcribers write the separator as a word, so "git commit dash m"
    and "no dash build" arrive in the same shape and only context separates
    them. The one that holds: a flag can only appear while the line is still
    naming what to run — the command and any subcommands ("git commit", "pip
    install", "tmux new"). Once an operand has been spoken the dash is
    hyphenating that operand's name, which is what "no dash build" and
    "conda dash forge" are.

    So the left context has to be nothing but bare command words, and the right
    side has to look like a flag rather than a word. Both halves are needed:
    without the shape test "git commit dash message" would lose its hyphen, and
    without the position test "flash dash attn" would gain a space.

    `command_only` additionally demands that the segment be headed by a name
    that really is a command. A dash carries its own evidence — nobody dictates
    one mid-sentence — but "plus" is an ordinary English word, so "the plus x in
    that sentence" would otherwise read its "the" as a command and rewrite
    prose.
    """
    if not _FLAG_SHAPED.match(right.strip(",.")):
        return False
    # An operator starts a fresh command, so only the words since the last one
    # count: in "ps aux | grep uvicorn | head dash n5" the flag belongs to
    # `head`, which is at command position in its own segment.
    prefix = out
    for j in range(len(out) - 1, -1, -1):
        if out[j] in OPERATORS:
            prefix = out[j + 1:]
            break
    # Every token in the segment a bare word, so nothing that is already an
    # operand: a path, a flag, an option's value, or a name ends the command
    # prefix. Two words of prefix past the command is the ceiling ("git stash
    # pop" style); beyond that the words are arguments, not subcommands.
    if not 1 <= len(prefix) <= 3:
        return False
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", tok.strip(",."))
               for tok in prefix):
        return False
    return not command_only or _known_shell_command(prefix[0].strip(",.").lower())


def _tokenize(text: str) -> list[str]:
    """Whitespace tokens, with em/en dashes split out so rules can see them."""
    spaced = text
    for dash in _EM_DASHES:
        spaced = spaced.replace(dash, " — ")
    for dash in _EN_DASHES:
        spaced = spaced.replace(dash, " – ")
    return spaced.split()


def apply_rules(text: str, register: str) -> str:
    """Rewrite spoken punctuation into the characters it names.

    Only applied where the surrounding words look like a command or identifier:
    "the dot at the end" must survive as prose. Outside the shell the table is
    reduced to the forms that cannot be a sentence — a leading "dash dash", and
    joiners between two identifier-shaped words.
    """
    tokens = _tokenize(text)
    if not tokens:
        return text

    strict = register != "shell"
    out: list[str] = []
    i = 0
    n = len(tokens)

    while i < n:
        tok = tokens[i]
        low = tok.lower().strip(",.")

        # A "cd" homophone is only the command where a command can stand, so the
        # rewrite is gated on that; "that seedy pattern" keeps its word.
        at_command = not out or out[-1] in ("|", ";", "&&", "||")
        if not strict and at_command and tok.lower() in CD_HOMOPHONES:
            out.append("cd")
            i += 1
            # "cd to work" and "cd into work" are how the directory is spoken;
            # the preposition is not part of the path.
            if len(out) == 1 and i < n \
                    and tokens[i].lower().strip(",.") in ("to", "into"):
                i += 1
            continue

        # "dash dash verbose" → "--verbose"; an em dash the phone substituted for
        # the same spoken words undoes to the identical thing.
        is_em = tok in _EM_DASHES
        is_en = tok in _EN_DASHES
        if (low == "dash" and i + 1 < n and tokens[i + 1].lower().strip(",.") == "dash") \
                or is_em:
            step = 1 if is_em else 2
            if i + step < n and _is_wordish(tokens[i + step]):
                out.append("--" + tokens[i + step].lower())
                i += step + 1
                continue
            i += step
            out.append("--")
            continue

        if (low == "dash" or is_en) and out and i + 1 < n:
            # A single dash either starts a short flag ("dash v") or hyphenates
            # two words ("no dash build" → "no-build"). Both need a word on the
            # right; the left decides which.
            right = tokens[i + 1]
            # A dictated dash between two ordinary words is the punctuation
            # mark, which prose keeps; hyphenating it would rewrite the
            # sentence rather than a token.
            prose_dash = (out[-1].lower().strip(",.") in COMMON_WORDS
                          and right.lower().strip(",.") in COMMON_WORDS)
            if _is_wordish(right) and not prose_dash:
                left = out[-1]
                if left.startswith("-") and len(left) > 1:
                    # Continuing a flag the rules already began: "--no" then
                    # "dash build" is one option, "--no-build".
                    out[-1] = left + "-" + right
                elif is_en or len(out) == 1 or _at_flag_position(out, right):
                    # A flag: either the phone substituted an en dash (which it
                    # only does where the user paused, i.e. between the command
                    # and its options), the left token is the command itself, or
                    # nothing but the command and its subcommands has been said
                    # yet and the right side is flag-shaped.
                    out.append("-" + right)
                elif _is_wordish(left):
                    # Mid-line between two words: hyphenating one name.
                    out[-1] = left + "-" + right
                else:
                    out.append("-" + right)
                i += 2
                continue

        if not strict and low == "slash" and i + 1 < n and _is_wordish(tokens[i + 1]):
            # "cd to slash is slash cluster": the first slash has nothing on its
            # left worth joining — a command word or a preposition is not a path
            # segment — so it opens an absolute path instead. The rest of the
            # chain is consumed here because "/is" is not wordish, and so would
            # not survive the joiner rule's left-hand test on the next pass.
            prev = out[-1].lower().strip(",.") if out else ""
            prev_at_command = len(out) == 1 or (len(out) > 1
                                                and out[-2] in ("|", ";", "&&", "||"))
            if not out or prev in _PATH_LEAD_WORDS \
                    or (prev_at_command and prev in _PATH_COMMANDS):
                # Whisper comma-separates list-ish dictation; strip each
                # consumed segment's trailing punctuation so it doesn't end up
                # embedded mid-token (e.g. "/is,/cluster").
                path = "/" + tokens[i + 1].rstrip(",.")
                i += 2
                while i + 1 < n and tokens[i].lower().strip(",.") == "slash" \
                        and _is_wordish(tokens[i + 1]):
                    path += "/" + tokens[i + 1].rstrip(",.")
                    i += 2
                out.append(path)
                continue

        if low == "plus" and not strict and out and i + 1 < n \
                and _at_flag_position(out, tokens[i + 1], command_only=True):
            # "chmod plus x" is the mode argument "+x", which stands apart from
            # the command the way a flag does. Everywhere else "plus" joins
            # ("c plus plus", "g plus plus"), so this is gated on exactly the
            # position that makes a dash a flag rather than a hyphen.
            out.append("+" + tokens[i + 1].strip(",."))
            i += 2
            continue

        if low in JOINERS and out and i + 1 < n:
            char = JOINERS[low]
            left, right = out[-1], tokens[i + 1]
            # "dot py" is a suffix even with nothing to its left worth joining;
            # every other joiner needs identifier-shaped words on both sides.
            ext_case = char == "." and right.lower().strip(",.") in EXTENSIONS
            # "star dot log" is the glob "*.log": a wildcard is not a word, but
            # an extension still attaches to it.
            if ext_case and left in ("*", "?"):
                out[-1] = left + char + right.lower().strip(",.")
                i += 2
                continue
            # "head tilde three" is HEAD~3: a number word directly right of a
            # ~, = or : is the digit, because nothing spells a number out there.
            num_case = right.lower() in NUMBER_WORDS and char in "~=:"
            # A chain that keeps going is an identifier being spelled out:
            # "test slash test underscore ..." names a file, and no sentence
            # puts a second punctuation name two words after the first. This is
            # what lets the guard below stay on for prose while a path built
            # out of ordinary words still joins.
            chain_case = i + 2 < n and tokens[i + 2].lower().strip(",.") in JOINERS
            # "the dot at the end" is someone describing punctuation, not naming
            # a file: joining two ordinary English words is never right unless
            # one of the shapes above says this is a token after all.
            prose = (left.lower().strip(",.") in COMMON_WORDS
                     and right.lower().strip(",.") in COMMON_WORDS
                     and not ext_case and not num_case and not chain_case)
            if _is_wordish(left) and _is_wordish(right) and not prose \
                    and (not strict or ext_case or num_case or char in "/_"):
                if ext_case:
                    right = right.lower().strip(",.")
                elif num_case:
                    right = NUMBER_WORDS[right.lower()]
                # Whisper comma-separates list-ish dictation; strip the left
                # side's trailing punctuation so it doesn't end up mid-token
                # (e.g. "cluster,/fast"). `right` is left alone — if the chain
                # continues it becomes a future left and gets stripped then; a
                # final segment's trailing punctuation is genuine sentence
                # punctuation and must survive.
                out[-1] = left.rstrip(",.") + char + right
                i += 2
                continue

        if not strict and low in STANDALONE and out and i + 1 < n:
            out.append(STANDALONE[low])
            i += 1
            continue

        if not strict and low in NUMBER_WORDS and out:
            # Digits only next to something already command-shaped: "three
            # commits ago" must stay spelled out.
            prev = out[-1]
            if prev.startswith("-") or prev.endswith(("~", "=", ":", "-")) \
                    or (prev.isupper() and len(prev) <= 6):
                if prev.endswith(("~", "=", ":", "-")):
                    out[-1] = prev + NUMBER_WORDS[low]
                else:
                    out.append(NUMBER_WORDS[low])
                i += 1
                continue

        out.append(tok)
        i += 1

    return " ".join(out)


# ---------------------------------------------------------------------------
# ASR-only normalization
# ---------------------------------------------------------------------------
# Corrections for what the acoustic model mishears, as opposed to what the
# speaker actually said. They run before apply_rules and ONLY on a whisper
# transcript: typed text means what it says, so rewriting a word the user chose
# to type would be a bug rather than a fix.

# whisper renders a spoken "pipe" as "pip" often enough to matter (the benchmark
# caught it repeatedly), because `pip` is a real word it has strong priors for.
# The rewrite is guarded on both sides, since the command is at least as common
# as the operator: a following subcommand or a preceding "upgrade" means the
# user really did say the package manager.
PIP_SUBCOMMANDS = frozenset("""
install uninstall list show freeze download config cache index wheel check
debug hash help
""".split())

# "upgrade pip" / "install pip" — the word is the object of the verb, not an
# operator. A lone "pip" with a command-shaped word after it is the pipe.
_PIP_LEFT_GUARD = frozenset("upgrade upgrading update install installing reinstall".split())


def _asr_pip_is_pipe(tokens: list[str], i: int) -> bool:
    """Whether the "pip" at `tokens[i]` is a misheard "pipe"."""
    nxt = tokens[i + 1].lower().strip(",.") if i + 1 < len(tokens) else ""
    if nxt in PIP_SUBCOMMANDS:
        return False
    prev = tokens[i - 1].lower().strip(",.") if i > 0 else ""
    if prev in _PIP_LEFT_GUARD:
        return False
    # Nothing on the right to pipe into is a sentence ending on the word, not an
    # operator; an operator always has both sides.
    return bool(nxt) and bool(prev)


# Each rule is (predicate over (tokens, i), replacement token). Kept as a table
# so the next mishearing the benchmark turns up is one entry, not one branch.
ASR_RULES = {
    "pip": (_asr_pip_is_pipe, "pipe"),
}


# Shell builtins whisper never sees on $PATH (they are handled by the shell
# itself, not an executable), plus the handful of common commands the model
# reliably capitalizes as if starting a sentence. $PATH covers the rest.
SHELL_BUILTINS = frozenset("""
cd ls cat cp mv rm echo export source kill top ssh git python pip
""".split())

# Whisper renders a shell line as a sentence: capitalized first word, trailing
# full stop. Neither belongs on a command line, but both are only safe to
# strip in the shell register — prose elsewhere relies on both.
_SENTENCE_END = frozenset((".", "?", "!"))

# git subcommands whisper's "Get"/"get" homophone is worth fixing in front of.
# Kept short and specific rather than exhaustive: a wrong guess here rewrites a
# command, so the list is the subcommands actually common in dictated lines.
GIT_SUBCOMMANDS = frozenset("""
push pull commit status rebase checkout switch add log diff clone stash
fetch merge branch reset restore tag remote init show blame cherry-pick
bisect worktree
""".split())


def _known_shell_command(word: str) -> bool:
    """Whether `word` (already lowercased) is a real command, not just a guess.

    $PATH is the ground truth for what the user can actually run; the builtins
    set fills in the handful of commands a shell handles itself and so never
    appear there.
    """
    return word in SHELL_BUILTINS or word in path_commands()


def _strip_sentence_dressing(text: str) -> str:
    """Undo whisper's sentence case and trailing full stop, shell register only.

    Whisper transcribes a command as if it were a sentence: "Get push
    --force-with-lease." A trailing ".", "?" or "!" is dropped unless it is the
    meaningful dot in a filename-like token ("app.py." loses only the final,
    sentence-ending dot, leaving "app.py" — the extension's dot is a second,
    earlier character and is untouched). The first word is lowercased only when
    doing so turns it into a real command; a capitalized word that matches
    nothing is left exactly as heard, since it is as likely to be a proper noun
    as a mistake.
    """
    tokens = text.split()
    if not tokens:
        return text

    last = tokens[-1]
    if last and last[-1] in _SENTENCE_END:
        # A bare trailing mark preceded by a letter is filename-like ("app.py.");
        # only the sentence-ending mark itself comes off, leaving the dot the
        # filename actually needs. Anything else (plain "status." / "?" alone)
        # loses the whole trailing token.
        if len(last) > 1 and last[-2].isalpha():
            tokens[-1] = last[:-1]
        else:
            tokens.pop()

    if tokens:
        first = tokens[0]
        stripped = first.strip(",.!?;:")
        low = stripped.lower()
        if stripped and stripped != low and _known_shell_command(low):
            tokens[0] = low + first[len(stripped):]

    return " ".join(tokens)


def _asr_get_is_git(tokens: list[str], i: int) -> bool:
    """Whether the "Get"/"get" at `tokens[i]` is a misheard "git".

    Confined to command position (index 0): "go get push notifications
    working" is not a git invocation just because "push" follows. Guarded on
    the subcommand to its right otherwise: whisper already writes "git"
    correctly much of the time, so checking the table — never a bare presence
    check — is what keeps this a no-op on that case.
    """
    if i != 0:
        return False
    nxt = tokens[i + 1].lower().strip(",.") if i + 1 < len(tokens) else ""
    return nxt in GIT_SUBCOMMANDS


ASR_COMMAND_RULES = {
    "get": (_asr_get_is_git, "git"),
}


def apply_asr_rules(text: str, register: str = "claude",
                    learned_path=None) -> str:
    """Undo known acoustic-model mishearings, before the spoken-syntax rules.

    The output is still spoken-form text ("pipe", not "|"): this pass only puts
    back the word the speaker said, and apply_rules turns it into a character
    exactly as it would have if whisper had heard it right the first time.

    `learned_path` overrides the learned-correction store, for tests; the
    default is the one store this machine keeps.

    The command-position and sentence-dressing rules are shell-only: they
    read a capitalized word or a trailing period as noise because a shell
    prompt is a command, never a sentence. Claude's prompt and an editor are
    prose far more often than not, so both are left completely alone there.
    """
    tokens = text.split()
    if not tokens:
        return text
    out = list(tokens)
    for i, tok in enumerate(tokens):
        rule = ASR_RULES.get(tok.lower().strip(",."))
        if rule and rule[0](tokens, i):
            out[i] = rule[1]
        if register == "shell":
            crule = ASR_COMMAND_RULES.get(tok.lower().strip(",."))
            if crule and crule[0](tokens, i):
                out[i] = crule[1]
    result = " ".join(out)
    # After the table, before the sentence dressing: a learned correction is
    # the same kind of repair as the table's entries — putting back the word
    # the speaker said — and the dressing rules should see the sentence as it
    # was meant, not as it was misheard.
    result = apply_learned_rules(result, learned_path)
    if register == "shell":
        result = _strip_sentence_dressing(result)
    return result


# ---------------------------------------------------------------------------
# Filesystem path snapping (ASR only)
# ---------------------------------------------------------------------------
# A dictated absolute path is the one place where the ground truth is not the
# screen or the index but the filesystem itself: "/is/claster/fast" is wrong in
# a way `ls /is` can settle. This pass walks a path token segment by segment and
# only ever rewrites a segment that does NOT exist, against the listing of a
# parent that does.

# Its own budget, separate from the request's: a network mount that has gone
# away must cost the transcript this much and no more.
PATH_SNAP_BUDGET_S = 0.20

# One directory's listing, capped. A home on a cluster filer can hold tens of
# thousands of entries, and the segment we are looking for is not more likely to
# be the ten-thousandth than the tenth.
MAX_SNAP_ENTRIES = 3000

# A snapped segment must clear the tightest register threshold — this pass has
# no sentence context to fall back on, only the fact that the word names nothing
# that exists. The metaphone rung of score_entry sits at 0.85, so this admits a
# same-sounding segment ("claster"/"cluster") and nothing weaker.
PATH_SNAP_MIN = max(THRESHOLDS.values())

# ...and must beat its runner-up by this much. Mirrors match_window's tie
# handling: where two entries explain the segment equally well, neither is
# evidence, so the walk stops rather than picking one.
PATH_SNAP_MARGIN = 0.05

# The character-overlap fallback, for segments no pronunciation explains — see
# _snap_segment. Measured against the real listing that produced this feature:
# the live model's "sdwedi" takes `sdwivedi` at 0.857 with a 0.324 margin, while
# every other model's garble of the same word ("stved", "stvved", "stivadi")
# fails both gates at once — 0.571-0.667 ratios and margins under 0.045. The
# thresholds sit in that gap rather than at the edge of either side of it.
PATH_SNAP_FALLBACK_MIN = 0.80
PATH_SNAP_FALLBACK_MARGIN = 0.10
PATH_SNAP_FALLBACK_MIN_CHARS = 5

_PATH_TOKEN_RE = re.compile(r"^(/|~/)")

# A relative path is only worth probing the filesystem for when the token could
# not be anything else: two or more segments around a real "/", each of them
# path-shaped. A bare word never qualifies — without this the snapper would put
# a directory listing behind every noun in a sentence — and neither does a
# spoken "either/or", whose segments are ordinary words that match nothing in
# any project directory. The evidence that settles it is the filesystem's, in
# _snap_walk; this pattern only decides what is cheap enough to ask about.
_REL_PATH_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+/?$")

# Trailing sentence punctuation is not part of the path. Stripped before every
# probe and reattached to whatever the walk produces.
_PATH_TRAIL = ",.?!"


def _snap_listing(directory: str, deadline: float) -> list[str]:
    """Entry names in `directory`, bounded and never raising.

    Read-only and non-following: scandir yields names without a stat, so a dead
    automount costs one failed open rather than a hung request.
    """
    if deadline and time.monotonic() > deadline:
        return []
    names: list[str] = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                names.append(entry.name)
                if len(names) >= MAX_SNAP_ENTRIES:
                    break
                if len(names) % 200 == 0 and deadline and time.monotonic() > deadline:
                    break
    except OSError:
        return []
    return names


def _snap_exists(path: str) -> bool:
    """lexists, never raising — a symlink into a dead mount is still a segment."""
    try:
        return os.path.lexists(path)
    except OSError:
        return False


def _snap_best(names: list[str], score) -> tuple[str, float, float]:
    """(best name, its score, the runner-up's) under `score`."""
    best, best_score, runner_up = "", 0.0, 0.0
    for name in names:
        value = score(name)
        if value > best_score:
            best, best_score, runner_up = name, value, best_score
        elif value > runner_up:
            runner_up = value
    return best, best_score, runner_up


def _snap_segment(parent: str, segment: str, deadline: float) -> str:
    """The entry of `parent` that `segment` was meant to be, or "".

    Two scorers, tried in order. First score_entry, against the same Entry
    shape the index uses, so a segment snaps here on exactly the evidence it
    would need anywhere else. That scorer is phonetic, and a username is the
    one path segment with no pronunciation to be right about: whisper renders
    `sdwivedi` as "sdwedi" or "stved", which share no metaphone key with it.

    So when the phonetic scorer finds nothing, character overlap gets a turn at
    a looser bar. That is only safe because of where it runs: the candidates are
    one real directory's listing rather than a vocabulary of guesses, so the
    answer is either in that closed set or the walk stops. The margin still has
    to be clear, and a short segment is excluded outright — at four characters
    an edit flips the ranking, and the ratio stops being evidence.

    An empty return means no confident match, which stops the walk.
    """
    names = _snap_listing(parent, deadline)
    if not names:
        return ""
    # Dictated digits survive transcription literally, so a segment and a
    # candidate that disagree on them are two different names rather than one
    # mishearing. Neither scorer sees digits — metaphone() drops them and the
    # ratio treats them as ordinary characters — so both need this guard.
    digits = [c for c in segment if c.isdigit()]
    names = [n for n in names if [c for c in n if c.isdigit()] == digits]
    if not names:
        return ""

    best, best_score, runner_up = _snap_best(
        names, lambda n: score_entry(segment, [segment], Entry(n, "cwd")))
    if best_score >= PATH_SNAP_MIN and best_score - runner_up >= PATH_SNAP_MARGIN:
        return best

    if len(segment) < PATH_SNAP_FALLBACK_MIN_CHARS:
        return ""
    low = segment.lower()
    best, best_score, runner_up = _snap_best(
        names, lambda n: difflib.SequenceMatcher(None, low, n.lower()).ratio())
    if best_score < PATH_SNAP_FALLBACK_MIN \
            or best_score - runner_up < PATH_SNAP_FALLBACK_MARGIN:
        return ""
    return best


def _snap_walk(token: str, home: str, deadline: float, cwd: str = "") -> str:
    """Rewrite the nonexistent segments of one path token; keep the rest verbatim.

    The walk stops at the first segment that neither exists nor snaps: past a
    prefix we could not establish, every deeper listing is of the wrong
    directory, so the remaining segments are kept exactly as spoken.

    Where the walk starts and what is written back are two different things. An
    absolute token is probed from "/" and keeps its "/"; a "~/" token is probed
    from the home directory but keeps the "~/" it was spoken with; a relative
    token is probed from `cwd` and gets no prefix at all. Conflating the two —
    prepending the probe root to the output — is what turned a relative
    "test/test_x.py" into "/est/test_x.py".
    """
    if token.startswith("~/"):
        lead, probe_root = "~/", home
    elif token.startswith("/"):
        lead, probe_root = "/", "/"
    else:
        # Relative, and only ever walked against a caller-supplied cwd: with no
        # cwd there is no directory this token is relative *to*, and probing the
        # server's own working directory would answer a question nobody asked.
        if not cwd:
            return token
        lead, probe_root = "", cwd
    rest = token[len(lead):]
    segments = [s for s in rest.split("/") if s]
    if not segments:
        return token

    # Probing "/" itself for a lone "/foo" in prose would put a directory
    # listing behind an ordinary sentence, so a single segment is only walked
    # when it already exists.
    if len(segments) < 2 and not _snap_exists(os.path.join(probe_root, segments[0])):
        return token

    out: list[str] = []
    prefix = probe_root
    for i, segment in enumerate(segments):
        if deadline and time.monotonic() > deadline:
            out.extend(segments[i:])
            break
        candidate = os.path.join(prefix, segment)
        if _snap_exists(candidate):
            out.append(segment)
            prefix = candidate
            continue
        snapped = _snap_segment(prefix, segment, deadline)
        if not snapped:
            out.extend(segments[i:])
            break
        out.append(snapped)
        prefix = os.path.join(prefix, snapped)
    return lead + "/".join(out)


def _snap_resolves_to_dir(token: str, home: str) -> str:
    """The filesystem directory `token` names, or "" — the merge's precondition."""
    probe = home + token[1:] if token.startswith("~/") else token
    try:
        return probe if os.path.isdir(probe) else ""
    except OSError:
        return ""


def _snap_merges(first: str, second: str, home: str, deadline: float) -> bool:
    """Whether whisper split one path into `first` and `second` at a space.

    Gated entirely on filesystem evidence: the left side must be a real
    directory, the right side's head must NOT exist at the root it was written
    against, and it must exist (or confidently snap) under the left. Two paths
    that are each valid from root are two paths.
    """
    base = _snap_resolves_to_dir(first, home)
    if not base:
        return False
    head = second[len("~/") if second.startswith("~/") else 1:].split("/")[0]
    if not head:
        return False
    if _snap_exists(os.path.join(home if second.startswith("~/") else "/", head)):
        return False
    if _snap_exists(os.path.join(base, head)):
        return True
    return bool(_snap_segment(base, head, deadline))


def snap_paths(text: str, budget: float = PATH_SNAP_BUDGET_S,
               cwd: str = "") -> str:
    """Correct dictated path tokens against the filesystem. Never raises.

    Runs after apply_rules, so a spoken "slash is slash cluster" has already
    become one token by the time it gets here. Anything that goes wrong — a
    hung mount, a permission error, the budget — leaves the text as it was.

    `cwd` is the directory the request came from, and it is what makes a
    relative token ("tests/test_x.py") checkable at all: without it such a
    token names nothing in particular and is passed through untouched. Only
    tokens shaped like a multi-segment path are considered, and the walk still
    has to find the first segment in that directory before it looks any deeper.
    """
    tokens = text.split()
    if not tokens:
        return text
    deadline = time.monotonic() + max(0.01, budget)
    try:
        home = os.path.expanduser("~")
        # A cwd that is not a readable directory is no anchor at all; dropping
        # it here makes every relative token a pass-through rather than a
        # question asked of a directory that cannot answer.
        base = cwd if cwd and os.path.isdir(cwd) else ""

        def is_path_token(tok: str) -> bool:
            return bool(_PATH_TOKEN_RE.match(tok)
                        or (base and _REL_PATH_TOKEN_RE.match(
                            tok.rstrip(_PATH_TRAIL))))

        out: list[str] = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if not is_path_token(token):
                out.append(token)
                i += 1
                continue
            body = token.rstrip(_PATH_TRAIL)
            trail = token[len(body):]
            # A split path is merged before the walk, so the walk sees the whole
            # thing and can carry a corrected prefix into the second half. The
            # merge reads both halves as rooted paths, so it stays confined to
            # the tokens that are ones.
            while _PATH_TOKEN_RE.match(body) and i + 1 < len(tokens) \
                    and _PATH_TOKEN_RE.match(tokens[i + 1]):
                nxt = tokens[i + 1]
                nxt_body = nxt.rstrip(_PATH_TRAIL)
                if not _snap_merges(body, nxt_body, home, deadline):
                    break
                body = body.rstrip("/") + "/" + nxt_body.lstrip("/")
                trail = nxt[len(nxt_body):]
                i += 1
            out.append(_snap_walk(body, home, deadline, base) + trail)
            i += 1
        return " ".join(out)
    except Exception:  # noqa: BLE001 — a snap failure must not cost the transcript
        return text


# ---------------------------------------------------------------------------
# Window matching
# ---------------------------------------------------------------------------

# Below this many characters a phonetic key carries no information — "h m" and
# "am" share a key, and so would half the index. Short windows may still be
# corrected, but only by an exact or separator-only match.
MIN_FUZZY_CHARS = 5


def score_entry(window: str, words: list[str], entry: Entry) -> float:
    """How well `entry` explains this run of spoken words, 0..1.

    The ladder is deliberate: identity first, then separator-only differences
    (the whole point of the index), then sound, then spelling. Only the top two
    rungs are trusted against a common English word, or against a window too
    short for its phonetic key to mean anything.
    """
    norm = normalize(window)
    if window.lower() == entry.surface.lower():
        return 1.0
    if norm and norm == entry.norm:
        return 0.95

    # Fuzzy matching needs enough letters on both sides to be evidence rather
    # than coincidence, and needs the two forms to be comparable in length: a
    # three-letter window is not a plausible slip of a twelve-letter filename.
    if len(norm) < MIN_FUZZY_CHARS or len(entry.norm) < MIN_FUZZY_CHARS:
        return 0.0
    if abs(len(norm) - len(entry.norm)) > max(3, len(entry.norm) // 3):
        return 0.0

    key = metaphone(norm)
    word_key = "".join(metaphone(w) for w in words)
    if len(key) >= 3 and (key == entry.key or key == entry.word_key):
        return 0.85
    if len(word_key) >= 3 and (word_key == entry.key or word_key == entry.word_key):
        return 0.85

    sim = similarity(norm, entry.norm)
    if sim >= 0.72:
        return sim * 0.9
    return 0.0


def _candidates(window: str, words: list[str], index: Index) -> list[Entry]:
    """The entries worth scoring for this window — everything else is noise.

    Scoring every entry against every window is what would blow the time budget
    on a big index, so phonetic keys pre-filter and only a short, length-similar
    slice reaches the edit-distance path.
    """
    norm = normalize(window)
    found: list[Entry] = []
    seen: set[int] = set()

    for key in (metaphone(norm), "".join(metaphone(w) for w in words)):
        for entry in index.by_key.get(key, [])[:12]:
            if id(entry) not in seen:
                seen.add(id(entry))
                found.append(entry)

    exact = index.by_norm.get(norm)
    if exact is not None and id(exact) not in seen:
        seen.add(id(exact))
        found.append(exact)

    # Edit-distance candidates: same first letter and a comparable length is a
    # cheap filter that keeps everything a one-or-two-character slip could be.
    if norm:
        budget = 0
        for entry in index.entries:
            if id(entry) in seen or not entry.norm:
                continue
            if entry.norm[0] != norm[0]:
                continue
            if abs(len(entry.norm) - len(norm)) > 2:
                continue
            found.append(entry)
            budget += 1
            if budget >= 60:
                break
    return found


_TECHNICAL_CHARS = re.compile(r"[_/.~=:|*&^+-]")


def _looks_technical(words: list[str]) -> bool:
    """Does this window show any sign of naming a thing rather than saying one?

    Fuzzy matching is only safe where there is such a sign. Without one, every
    ordinary word in a sentence is a candidate for the nearest filename, which
    is how "review" becomes "rview" — the failure this gate exists to prevent.

    The signs are the residue of dictated code: punctuation the rule pass
    assembled, a digit, internal capitals, or a spelled-out single letter.
    Membership of the common-word list is deliberately NOT a sign — that list
    has holes by construction, and treating "not in the list" as evidence would
    hand every uncommon English word straight back to the fuzzy matcher.
    """
    for word in words:
        bare = word.strip(",.!?;:")
        if not bare:
            continue
        if _TECHNICAL_CHARS.search(bare):
            return True
        if any(c.isdigit() for c in bare):
            return True
        if bare[1:] != bare[1:].lower():  # internal capital: camelCase, HTTPServer
            return True
        # A spelled-out letter ("camera h m r"), but never the articles "a" and
        # "I", which are single letters in every ordinary sentence and would
        # otherwise mark half of English as technical.
        if len(bare) == 1 and bare.isalpha() and bare.lower() not in ("a", "i"):
            return True
    return False


def match_window(window: str, words: list[str], index: Index, register: str,
                 threshold: float, at_command: bool = True
                 ) -> tuple[str, float, list[str], str]:
    """Best replacement for this window: (surface, score, alternates, source).

    Returns an empty surface when nothing clears the bar — which is the common
    and desirable outcome.
    """
    # Protected when the window carries no sign of being technical: a run of
    # ordinary English, or a single plain word, is overwhelmingly more likely to
    # be the sentence the user actually said than a mispronounced identifier, so
    # only an exact or separator-only match may touch it. This is the rule that
    # keeps prose passing through byte-identical, and it is worth more than any
    # correction it costs.
    # In the shell the whole line is a command, so a plain-looking window is
    # still fair game and only a common English word is held back. Everywhere
    # else the user is writing sentences, and a window must show a technical
    # sign before fuzzy matching may touch it at all.
    if register == "shell":
        protected = all(w.lower().strip(",.!?;:") in COMMON_WORDS for w in words)
    else:
        protected = not _looks_technical(words)
    floor = max(threshold, PROTECTED_MIN if protected else 0.0)
    norm = normalize(window)

    scored: list[tuple[float, int, int, Entry]] = []
    for entry in _candidates(window, words, index):
        # An entry identical to what was said is not a correction. Saying
        # "test" where a `test` binary exists must leave the word alone, not
        # "replace" it with itself and report a span the user has to dismiss.
        if entry.surface.lower() == window.lower():
            return "", 0.0, [], ""
        score = score_entry(window, words, entry)
        # $PATH is thousands of names, most of them obscure enough that some
        # binary sounds like any given English word (rview, tload, oclock). It
        # is a good source for what the user is *running* and a terrible one for
        # everything else, so a fuzzy hit on it only counts in command position.
        # Shell history is the same kind of source and needs most of the same
        # handling — hundreds of names the user is not currently looking at,
        # among which something sounds like almost any English word
        # ("loading"/`left.png`, "a timeout"/`auto-mode`). It broadens what the
        # index can reach; it must not gain the right to overrule ordinary
        # prose to do it.
        #
        # The one difference from $PATH: a $PATH entry is only a plausible
        # reading in command position because it names something to *run*,
        # while a remembered path is plausible wherever a path is spoken —
        # "go to the folder called /is/cluster/fast/sdwivedi" is the case this
        # source exists for, and it is nowhere near a command position. So the
        # position rule is applied to history entries that look like bare
        # commands, and lifted for the ones that carry a path's separators.
        #
        # The exception, for both sources: a multi-word window whose letters
        # spell the entry exactly. A verbatim transcriber writes a compound name
        # as the words it sounds like ("camera hmr", "interact vlm"), and those
        # are the same letters in the same order as the name — evidence of a
        # different kind from the phonetic near-miss this guard exists to stop.
        # One spoken word matching one remembered command is the dangerous case
        # and stays guarded; the merge of several into a name that is spelled
        # that way is not a guess about what a word sounded like.
        spelled_merge = len(words) > 1 and norm == entry.norm
        if not spelled_merge and (
                entry.source == "path"
                or (entry.source == "history"
                    and not _PATH_SHAPED_RE.search(entry.surface))):
            if not at_command and score < 1.0:
                continue
        # The metaphone-equality tier (score_entry's 0.85 rung) is a phonetic
        # guess, and $PATH is thousands of names — long enough that some
        # binary collides on sound with almost any multi-word window ("cat
        # app.py" and "cat file" both key the same as obscure binaries like
        # `gftype`/`keytool`). Three guards keep that guess from outrunning
        # its evidence, matching the same reasoning the length filter in
        # score_entry already applies, just tightened for this source/tier:
        if entry.source in _WIDE_SOURCES and score == 0.85:
            # (a) A window's first word that is ALREADY a verbatim index
            # entry (here, `cat` itself sits on $PATH) is not a mistake to
            # fix — the multi-word window it anchors must not overrule that
            # by guessing at the words as a whole.
            if index.has(words[0]):
                continue
            # (b) Sounding alike is not enough on its own; the spelling must
            # also be in the neighborhood. "pie test"/"pytest" (~0.71) and
            # "num pie"/"numpy" (~0.67) clear this easily; "cat file"/
            # "keytool" (~0.14) and "cat app.py"/"gftype" (~0.25) do not.
            if similarity(norm, entry.norm) < 0.5:
                continue
            # (c) A joined window much longer or shorter than the candidate
            # is a coincidence, not a slip. score_entry's general-purpose
            # length filter (max(3, len // 3)) is sized for the common case
            # of one identifier against one mis-transcribed identifier; this
            # tier is specifically a multi-word window against a single
            # $PATH token, so it gets a tighter, ratio-based ceiling instead
            # of that filter's constant-3 floor.
            if abs(len(norm) - len(entry.norm)) > max(2, len(entry.norm) // 3):
                continue
        if score >= floor:
            # Third key: when score and source tie, the surface carrying less
            # punctuation wins. `pytest` and `py.test` sound identical and both
            # sit on $PATH; the plain one is what people type.
            scored.append((score, Index.rank(entry),
                           -sum(not c.isalnum() for c in entry.surface), entry))
    if not scored:
        return "", 0.0, [], ""

    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    best_score, _, _, best = scored[0]
    alternates: list[str] = []
    for score, _, _, entry in scored[1:]:
        if entry.surface != best.surface and entry.surface not in alternates:
            alternates.append(entry.surface)
        if len(alternates) >= 3:
            break
    return best.surface, best_score, alternates, best.source


def _merge_path_tail(tokens: list[str], i: int, index: Index, register: str,
                     threshold: float) -> tuple[str, int] | None:
    """Rejoin a compound name the rules split across a path separator.

    "pretrained underscore models slash camera hmr" leaves the joiner pass as
    "pretrained_models/camera hmr": the slash claimed the first half of the
    name and the second half was left standing beside it. The whole token is
    not a window the matcher can use — the path prefix is part of it — so the
    merge is tried on the path's last segment alone, and only the segment is
    rewritten.

    The bar is the same one the spelled-merge exception sets: the segment plus
    the words after it have to spell a known name exactly. That is what
    separates this from guessing that a word after a path belongs to it.
    """
    token = tokens[i]
    head, sep, tail = token.rpartition("/")
    if not sep or not tail or not _is_wordish(tail):
        return None
    for size in range(min(3, len(tokens) - i - 1), 0, -1):
        rest = tokens[i + 1:i + 1 + size]
        if any(w in OPERATORS or w.startswith("-") for w in rest):
            continue
        # The name's second half may itself have been claimed by the next
        # separator — "interact vlm slash outputs" leaves "vlm/outputs" — so the
        # last of the words being merged contributes only its leading segment,
        # and whatever the separator took stays on the far side of the join.
        last, lsep, after = rest[-1].partition("/")
        words = [tail] + rest[:-1] + [last]
        if not last or not _is_wordish(last):
            continue
        surface, score, _, _ = match_window(" ".join(words), words, index,
                                            register, threshold, False)
        # An exact spelling merge only: score_entry's top rung, reached because
        # the letters line up, not because the sound was close.
        if surface and score >= 0.95 and normalize(" ".join(words)) == normalize(surface):
            return head + sep + surface + lsep + after, size + 1
    return None


def resolve_tokens(text: str, index: Index, register: str,
                   deadline: float = 0.0) -> tuple[str, list[dict]]:
    """Replace runs of 1..5 words with the identifiers they were meant to be.

    Longest window first, left to right: "camera h m r dot py" must be tried as
    a whole before its first word is matched against something shorter. A window
    that is already an index entry is left alone — it is not a mistake to fix.
    """
    tokens = text.split()
    if not tokens:
        return text, []

    threshold = THRESHOLDS.get(register, 0.85)
    out: list[str] = []
    spans: list[dict] = []
    i = 0
    n = len(tokens)

    while i < n:
        if deadline and time.monotonic() > deadline:
            out.extend(tokens[i:])
            break

        best: tuple[int, str, float, list[str], str] | None = None
        # Five, not four: the rule pass glues "tests slash test underscore
        # camera" into one token, so the spelled-out letters that follow
        # ("h m r.py") need a window long enough to reach back over it.
        for size in range(min(5, n - i), 0, -1):
            words = tokens[i:i + size]
            window = " ".join(words)
            if index.has(window):
                break
            # A flag the rule pass built is a finished answer; guessing at it
            # would undo the one thing the rules got right.
            if any(w.startswith("-") for w in words):
                continue
            # Nor may a window swallow an operator the rules just produced.
            # normalize() drops punctuation, so "| grep" reads as exactly the
            # word `grep` and scores 0.95 against it — the "correction" would
            # delete the pipe the user asked for.
            if any(w in OPERATORS for w in words):
                continue
            # Command position: the start of the line, or just after something
            # that ends one (a pipe, a semicolon, &&). Only there is a $PATH
            # name a plausible reading of an English-sounding word.
            at_command = i == 0 or (bool(out) and out[-1] in ("|", ";", "&&", "||"))
            surface, score, alternates, source = match_window(
                window, words, index, register, threshold, at_command)
            if not surface or surface.lower() == window.lower():
                continue
            if best is None or score > best[2]:
                best = (size, surface, score, alternates, source)
            # A long window that matched well is what we came for; a shorter one
            # cannot beat it on evidence, only on luck.
            if score >= 0.95:
                break

        if best is None:
            merged = _merge_path_tail(tokens, i, index, register, threshold)
            if merged is not None:
                surface, size = merged
                start = sum(len(t) + 1 for t in out)
                out.append(surface)
                spans.append({
                    "start": start, "end": start + len(surface),
                    "original": " ".join(tokens[i:i + size]),
                    "alternates": [], "source": "rule",
                })
                i += size
                continue
            out.append(tokens[i])
            i += 1
            continue

        size, surface, score, alternates, source = best
        start = sum(len(t) + 1 for t in out)
        out.append(surface)
        spans.append({
            "start": start,
            "end": start + len(surface),
            "original": " ".join(tokens[i:i + size]),
            "alternates": alternates[:3],
            "source": "phonetic" if score < 0.95 else "rule",
        })
        i += size

    return " ".join(out), spans


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _shift_spans(spans: list[dict], text: str) -> list[dict]:
    """Drop spans whose recorded offsets no longer point at their own text.

    Cheap insurance: a span the client would use to highlight the wrong
    characters is worse than no span, and the phases above each rebuild the
    string, so an offset can only be stale by a bug.
    """
    kept = []
    for span in spans:
        start, end = int(span["start"]), int(span["end"])
        if 0 <= start <= end <= len(text):
            kept.append(span)
    return kept


def resolve(text: str, screen=None, cwd: str = "", tmux_names=None,
            budget: float = TIME_BUDGET_S, asr: bool = False,
            extra_vocab=None, learned_path=None) -> dict:
    """Correct one dictated line. Never raises; on any trouble, echoes the input.

    The phases run rules → matching, each over the output of the last, so span
    offsets are always indices into the text as it stands at the end. Whatever
    goes wrong — a bad screen payload, a filesystem that hangs, a clock past the
    budget — the caller gets its text back and the user simply sees no
    correction.

    `asr` marks the text as a speech-recognition transcript rather than
    something a person typed, which admits the ASR_RULES pass — see
    apply_asr_rules for why that distinction has to be made by the caller.

    `extra_vocab` widens the phonetic index with words the caller supplies
    (shell history from the transcribe path). It only ever adds candidates:
    every threshold, the common-English-word protection and the per-register
    tightening apply to them exactly as they do to a filename off the screen.

    Words the user has taught by editing past transcripts join `extra_vocab` on
    the same terms. `learned_path` overrides the store they come from, for
    tests; the default is the one store this machine keeps.
    """
    raw = str(text or "")
    fallback = {"text": raw, "register": "claude", "spans": []}
    if not raw.strip():
        return fallback

    started = time.monotonic()
    deadline = started + max(0.05, budget)
    try:
        register = detect_register(screen)

        current = apply_asr_rules(raw, register, learned_path) if asr else raw
        spans: list[dict] = []

        if time.monotonic() > deadline:
            return {"text": current, "register": register, "spans": []}

        ruled = apply_rules(current, register)
        if ruled != current:
            spans = _rule_spans(current, ruled)
            current = ruled

        # After the rules, so spoken slashes are already one token; before the
        # index, so a path the filesystem settled is not second-guessed by a
        # phonetic match against the screen.
        if asr:
            snapped = snap_paths(current, cwd=cwd)
            if snapped != current:
                spans = _merge_spans(spans, _rule_spans(current, snapped),
                                     current, snapped)
                current = snapped

        if time.monotonic() > deadline:
            return {"text": current, "register": register,
                    "spans": _shift_spans(spans, current)}

        # A word the user has corrected by hand twice is vocabulary this
        # machine has evidence for, on exactly the footing of a word off the
        # screen: it widens the candidate pool and clears the same thresholds.
        vocab = list(extra_vocab or []) + learned_words(learned_path)
        index = build_index(screen=screen, cwd=cwd, tmux_names=tmux_names,
                            deadline=deadline, extra_vocab=vocab)
        final, token_spans = resolve_tokens(current, index, register, deadline)

        if final != current:
            spans = _merge_spans(spans, token_spans, current, final)
            current = final
        return {"text": current, "register": register,
                "spans": _shift_spans(spans, current)}
    except Exception:  # noqa: BLE001 — a resolver bug must not break dictation
        return fallback


def _rule_spans(before: str, after: str) -> list[dict]:
    """Spans for the words the rule pass changed, offsets into `after`."""
    a, b = before.split(), after.split()
    spans: list[dict] = []
    # The rules only ever merge tokens left to right, so walking `after` and
    # consuming as many `before` tokens as it takes to rebuild each one is an
    # exact reconstruction rather than a guess.
    ai = 0
    pos = 0
    for token in b:
        consumed: list[str] = []
        target = normalize(token)
        acc = ""
        while ai < len(a) and (normalize(acc) != target or not consumed):
            consumed.append(a[ai])
            acc += a[ai]
            ai += 1
            if normalize(acc) == target:
                break
        spoken = " ".join(consumed)
        if spoken and spoken != token:
            spans.append({
                "start": pos, "end": pos + len(token), "original": spoken,
                "alternates": [], "source": "rule",
            })
        pos += len(token) + 1
    return spans


def _merge_spans(previous: list[dict], new: list[dict], before: str,
                 after: str) -> list[dict]:
    """Combine earlier spans with the token pass's, all keyed to the final text.

    Earlier spans are re-found by searching the final text for the exact
    substring they covered before: the token pass rewrites the string, so their
    recorded offsets are stale, but the text they produced is usually still
    there and still worth a chip. One that cannot be found again, or that now
    overlaps a token-pass span, is dropped — a span pointing at the wrong
    characters is worse than no span at all.
    """
    merged = list(new)
    taken = [(s["start"], s["end"]) for s in new]
    for span in previous:
        surface = before[span["start"]:span["end"]]
        if not surface:
            continue
        at = after.find(surface)
        if at < 0:
            continue
        start, end = at, at + len(surface)
        if any(start < e and s < end for s, e in taken):
            continue
        moved = dict(span)
        moved["start"], moved["end"] = start, end
        merged.append(moved)
        taken.append((start, end))
    merged.sort(key=lambda s: s["start"])
    return merged
