"""macOS integration via PyObjC (all local, no network).

* ``MacSpeller``: the system spelling service (``NSSpellChecker``).
* ``unify_title_bar``: puts the app toolbar into the title bar the way Finder,
  Preview and Xcode do (52 pt band, traffic lights vertically centred).
"""
from __future__ import annotations

import sys

IS_MAC = sys.platform == "darwin"


class MacSpeller:
    name = "macOS spelling"

    def __init__(self):
        from AppKit import NSSpellChecker  # noqa: import inside: optional dependency

        self._checker = NSSpellChecker.sharedSpellChecker()
        self._tag = NSSpellChecker.uniqueSpellDocumentTag()
        self._cache: dict[str, bool] = {}
        self.available = True
        # Automatic language identification cannot classify a single word (it
        # then reports nothing), and languages without an installed dictionary
        # accept everything, so check against the primary spelling language.
        try:
            lang = str(self._checker.language() or "")
        except Exception:
            lang = ""
        self.languages = [lang or "en"]

    def check(self, word: str) -> bool:
        cached = self._cache.get(word)
        if cached is not None:
            return cached
        ok = False
        try:
            for lang in self.languages:
                rng, _count = self._checker.checkSpellingOfString_startingAt_language_wrap_inSpellDocumentWithTag_wordCount_(
                    word, 0, lang, False, self._tag, None)
                if rng.length == 0:
                    ok = True
                    break
        except Exception:
            ok = True
        if len(self._cache) > 20000:
            self._cache.clear()
        self._cache[word] = ok
        return ok

    def suggest(self, word: str, limit: int = 6) -> list[str]:
        try:
            guesses = self._checker.guessesForWordRange_inString_language_inSpellDocumentWithTag_(
                (0, len(word)), word, self.languages[0], self._tag) or []
            return [str(g) for g in guesses][:limit]
        except Exception:
            return []

    def learn(self, word: str):
        try:
            self._checker.learnWord_(word)
        except Exception:
            pass
        self._cache[word] = True

    def ignore(self, word: str):
        try:
            self._checker.ignoreWord_inSpellDocumentWithTag_(word, self._tag)
        except Exception:
            pass
        self._cache[word] = True

    def close(self):
        try:
            self._checker.closeSpellDocumentWithTag_(self._tag)
        except Exception:
            pass


def make_speller():
    if not IS_MAC:
        return None
    try:
        return MacSpeller()
    except Exception:
        return None


def _nswindow(widget):
    # winId() is an NSView pointer only on the native "cocoa" platform plugin
    # (not under offscreen/minimal used by tests); never dereference it otherwise.
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.platformName() != "cocoa":
        return None
    try:
        import objc
        from AppKit import NSView

        view = objc.objc_object(c_void_p=int(widget.winId()))
        if isinstance(view, NSView):
            return view.window()
    except Exception:
        return None
    return None


def unify_title_bar(widget) -> bool:
    """Transparent 52-pt title bar with centred traffic lights over our toolbar.

    Returns False (and changes nothing) if AppKit is unavailable, in which case
    the caller keeps a normal title bar.
    """
    if not IS_MAC:
        return False
    try:
        from AppKit import (
            NSToolbar, NSWindowStyleMaskFullSizeContentView, NSWindowTitleHidden, NSWindowToolbarStyleUnified,
        )
    except Exception:
        return False
    win = _nswindow(widget)
    if win is None:
        return False
    try:
        toolbar = NSToolbar.alloc().initWithIdentifier_("ENGDrawingViewerMain")
        toolbar.setShowsBaselineSeparator_(False)
        win.setToolbar_(toolbar)
        win.setToolbarStyle_(NSWindowToolbarStyleUnified)
        win.setTitleVisibility_(NSWindowTitleHidden)
        win.setTitlebarAppearsTransparent_(True)
        win.setStyleMask_(win.styleMask() | NSWindowStyleMaskFullSizeContentView)
        win.setMovableByWindowBackground_(False)
        return True
    except Exception:
        return False


def set_represented_file(widget, path: str | None):
    """Keep the document path on the window (Mission Control, Window menu)."""
    win = _nswindow(widget)
    if win is None:
        return
    try:
        win.setRepresentedFilename_(path or "")
    except Exception:
        pass
