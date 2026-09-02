import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import resolver as R  # noqa: E402


# The resolver reads $PATH for command vocabulary, so a test that resolves
# "git status" is at the mercy of whatever the machine — or the git hook
# running the suite, which puts git-core with its `git-status` on the front of
# $PATH — happens to have installed. Pin the vocabulary to a fixed list of the
# commands any shell has instead.
PATH_COMMANDS = """
awk bash cat chmod chown clear cp curl cut date df diff du echo env find gcc
git grep gzip head htop kill less ln ls make man mkdir mv nano node npm npx
pip pip3 ps pytest python python3 rm rmdir rsync scp sed sort ssh tail tar
tmux top touch tree uniq vim wc wget which xargs zsh
""".split()


@pytest.fixture(autouse=True)
def pinned_path_commands(monkeypatch):
    monkeypatch.setattr(R, "path_commands", lambda deadline=0.0: list(PATH_COMMANDS))
