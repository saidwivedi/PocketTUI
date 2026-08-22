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
# the filesystem, and both beat the long tail of $PATH.
SOURCE_PRIORITY = {
    "screen": 5,
    "cwd": 4,
    "branch": 3,
    "path": 2,
    "tmux": 1,
}

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


def screen_tokens(lines) -> list[str]:
    """Identifier-shaped words from the terminal buffer, original casing kept.

    Case is preserved because the screen is the one source that shows how the
    user's own project spells things — `CameraHMR` is not `camerahmr`.
    """
    tokens: list[str] = []
    for line in list(lines or [])[-MAX_SCREEN_LINES:]:
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
    """
    now = time.monotonic()
    hit = _cwd_cache.get(cwd)
    if hit and now - hit[0] < CWD_CACHE_TTL_S:
        return hit[1], hit[2]
    if deadline and now > deadline:
        return [], []
    names = walk_names(cwd, deadline)
    branches = [] if (deadline and time.monotonic() > deadline) else git_branches(cwd, deadline)
    # Unbounded growth is not a risk worth code, but a long-running server
    # visiting many cwds should not keep every one forever. The check and the
    # clear must not interleave across threads, or a clear from one racing
    # request can drop an entry a second request just added.
    with _cwd_cache_lock:
        if len(_cwd_cache) > 32:
            _cwd_cache.clear()
        _cwd_cache[cwd] = (now, names, branches)
    return names, branches


def build_index(screen=None, cwd: str = "", tmux_names=None,
                deadline: float = 0.0) -> Index:
    """Assemble the vocabulary for one request from every source available.

    `deadline` bounds the expensive sources — the cwd walk, git branches, a
    cold $PATH scan. Screen tokens are cheap and always included; everything
    else is skipped once the deadline has passed, which just means a thinner
    vocabulary rather than a slow answer.
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
                elif is_en or len(out) == 1:
                    # A flag: either the phone substituted an en dash (which it
                    # only does where the user paused, i.e. between the command
                    # and its options), or the left token is the command itself.
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
                path = "/" + tokens[i + 1]
                i += 2
                while i + 1 < n and tokens[i].lower().strip(",.") == "slash" \
                        and _is_wordish(tokens[i + 1]):
                    path += "/" + tokens[i + 1]
                    i += 2
                out.append(path)
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
            # "the dot at the end" is someone describing punctuation, not naming
            # a file: joining two ordinary English words is never right unless
            # one of the shapes above says this is a token after all.
            prose = (left.lower().strip(",.") in COMMON_WORDS
                     and right.lower().strip(",.") in COMMON_WORDS
                     and not ext_case and not num_case)
            if _is_wordish(left) and _is_wordish(right) and not prose \
                    and (not strict or ext_case or num_case or char in "/_"):
                if ext_case:
                    right = right.lower().strip(",.")
                elif num_case:
                    right = NUMBER_WORDS[right.lower()]
                out[-1] = left + char + right
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


def apply_asr_rules(text: str, register: str = "claude") -> str:
    """Undo known acoustic-model mishearings, before the spoken-syntax rules.

    The output is still spoken-form text ("pipe", not "|"): this pass only puts
    back the word the speaker said, and apply_rules turns it into a character
    exactly as it would have if whisper had heard it right the first time.

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
    if register == "shell":
        result = _strip_sentence_dressing(result)
    return result


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
        if entry.source == "path" and not at_command and score < 1.0:
            continue
        # The metaphone-equality tier (score_entry's 0.85 rung) is a phonetic
        # guess, and $PATH is thousands of names — long enough that some
        # binary collides on sound with almost any multi-word window ("cat
        # app.py" and "cat file" both key the same as obscure binaries like
        # `gftype`/`keytool`). Three guards keep that guess from outrunning
        # its evidence, matching the same reasoning the length filter in
        # score_entry already applies, just tightened for this source/tier:
        if entry.source == "path" and score == 0.85:
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
            budget: float = TIME_BUDGET_S, asr: bool = False) -> dict:
    """Correct one dictated line. Never raises; on any trouble, echoes the input.

    The phases run rules → matching, each over the output of the last, so span
    offsets are always indices into the text as it stands at the end. Whatever
    goes wrong — a bad screen payload, a filesystem that hangs, a clock past the
    budget — the caller gets its text back and the user simply sees no
    correction.

    `asr` marks the text as a speech-recognition transcript rather than
    something a person typed, which admits the ASR_RULES pass — see
    apply_asr_rules for why that distinction has to be made by the caller.
    """
    raw = str(text or "")
    fallback = {"text": raw, "register": "claude", "spans": []}
    if not raw.strip():
        return fallback

    started = time.monotonic()
    deadline = started + max(0.05, budget)
    try:
        register = detect_register(screen)

        current = apply_asr_rules(raw, register) if asr else raw
        spans: list[dict] = []

        if time.monotonic() > deadline:
            return {"text": current, "register": register, "spans": []}

        ruled = apply_rules(current, register)
        if ruled != current:
            spans = _rule_spans(current, ruled)
            current = ruled

        if time.monotonic() > deadline:
            return {"text": current, "register": register,
                    "spans": _shift_spans(spans, current)}

        index = build_index(screen=screen, cwd=cwd, tmux_names=tmux_names,
                            deadline=deadline)
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
