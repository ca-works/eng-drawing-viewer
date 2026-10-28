"""Offline spell checking.

``WordListSpeller`` uses the bundled ``english_words.txt`` (≈140k words from SCOWL)
plus words the user adds (saved in the settings). On macOS
``platform.macos.MacSpeller`` and on Windows ``platform.windows_spell.WindowsSpeller``
use the system spelling service instead (also fully local), which follows the
user's languages and learned words.
"""
from __future__ import annotations

import re
import string
import threading
from pathlib import Path

WORD_RE = re.compile(r"[^\W\d_][^\W\d_'’-]*(?:['’-][^\W\d_]+)*", re.UNICODE)


def checkable(word: str) -> bool:
    """Skip tokens that are almost never prose: acronyms, single letters, codes."""
    if len(word) <= 1:
        return False
    if word.isupper():
        return False  # GD&T, ISO, DWG, MAX …
    if any(c.isupper() for c in word[1:]) and not word[1:].isupper():
        return False  # camelCase / part codes like iPhone, McAdam
    return True


class WordListSpeller:
    name = "Built-in dictionary"

    def __init__(self, words_path: Path, user_words: set[str] | None = None, on_learn=None):
        self._path = Path(words_path)
        self._on_learn = on_learn  # called with the sorted user words after "Learn Spelling"
        self._words: set[str] | None = None
        self._user = {w.lower() for w in (user_words or set())}
        self._ignored: set[str] = set()
        self._lock = threading.Lock()

    def preload_async(self):
        threading.Thread(target=self._ensure, name="dictionary-load", daemon=True).start()

    def _ensure(self) -> set[str]:
        with self._lock:
            if self._words is None:
                words: set[str] = set()
                # The general dictionary plus a short list of drafting terms.
                for path in (self._path, self._path.with_name("engineering_words.txt")):
                    try:
                        text = path.read_text(encoding="utf-8")
                        words.update(w.strip().lower() for w in text.splitlines() if w.strip())
                    except OSError:
                        pass
                self._words = words
            return self._words

    @property
    def available(self) -> bool:
        return bool(self._ensure())

    def _known(self, w: str) -> bool:
        words = self._ensure()
        if w in words or w in self._user or w in self._ignored:
            return True
        if w.endswith("'s") and w[:-2] in words:
            return True
        if w.endswith("s") and (w[:-1] in words or (w.endswith("es") and w[:-2] in words)):
            return True
        return False

    def check(self, word: str) -> bool:
        w = word.replace("’", "'").lower().strip("'-")
        if not w or not self._ensure():
            return True
        if self._known(w):
            return True
        if "-" in w:
            return all(self._known(p) for p in w.split("-") if p)
        return False

    def suggest(self, word: str, limit: int = 6) -> list[str]:
        w = word.replace("’", "'").lower()
        words = self._ensure()
        letters = string.ascii_lowercase

        def edits1(s):
            splits = [(s[:i], s[i:]) for i in range(len(s) + 1)]
            deletes = [a + b[1:] for a, b in splits if b]
            transposes = [a + b[1] + b[0] + b[2:] for a, b in splits if len(b) > 1]
            replaces = [a + c + b[1:] for a, b in splits if b for c in letters]
            inserts = [a + c + b for a, b in splits for c in letters]
            return set(deletes + transposes + replaces + inserts)

        e1 = edits1(w)
        found = sorted((c for c in e1 if c in words), key=lambda c: (abs(len(c) - len(w)), c))
        if len(found) < limit and len(w) <= 14:
            e2 = {c2 for c1 in e1 for c2 in edits1(c1)} - e1
            found += sorted((c for c in e2 if c in words), key=lambda c: (abs(len(c) - len(w)), c))[: limit - len(found)]
        out = []
        for c in found[:limit]:
            if word[:1].isupper():
                c = c[:1].upper() + c[1:]
            out.append(c)
        return out

    def learn(self, word: str):
        self._user.add(word.lower())
        if self._on_learn is not None:
            self._on_learn(self.user_words())

    def ignore(self, word: str):
        self._ignored.add(word.lower())

    def user_words(self) -> list[str]:
        return sorted(self._user)
