"""Spell checking with Windows' own spelling service (Windows 8 and later, fully local).

Uses the Spell Checking API in the user's display language (English if that language has no
dictionary). Words added with "Learn Spelling" go into the user's Windows dictionary, so they are
remembered across sessions and shared with other apps that use it. ``make_speller`` returns None
when the service isn't available, and the app falls back to its built-in word list.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from . import wincom

CLSID_SpellCheckerFactory = "{7AB36653-1796-484B-BDFA-E74F1DB7C1DC}"
IID_ISpellCheckerFactory = "{8E018A9D-2415-4677-BF08-794EA61F94BB}"
# ISpellCheckerFactory
FACTORY_IS_SUPPORTED, FACTORY_CREATE = 4, 5
# ISpellChecker
CHECKER_CHECK, CHECKER_SUGGEST, CHECKER_ADD, CHECKER_IGNORE = 4, 5, 6, 7
# IEnumSpellingError / ISpellingError / IEnumString
ENUM_NEXT = 3
ERROR_CORRECTIVE_ACTION = 5
S_OK = 0


def _strings(enum: int, limit: int) -> list[str]:
    ole32 = ctypes.WinDLL("ole32")
    out = []
    while len(out) < limit:
        item = ctypes.c_void_p()
        fetched = wintypes.ULONG()
        hr = wincom.method(enum, ENUM_NEXT, ctypes.c_long, wintypes.ULONG, ctypes.POINTER(ctypes.c_void_p),
                           ctypes.POINTER(wintypes.ULONG))(1, ctypes.byref(item), ctypes.byref(fetched))
        if hr != S_OK or not fetched.value or not item.value:
            break
        out.append(ctypes.wstring_at(item.value))
        ole32.CoTaskMemFree(item)
    return out


class WindowsSpeller:
    name = "Windows spelling"

    def __init__(self, language: str | None = None):
        wincom.initialize()
        self._factory = wincom.create_instance(CLSID_SpellCheckerFactory, IID_ISpellCheckerFactory)
        self._checker = None
        for tag in (language, "en-US", "en-GB"):
            if tag and self._supported(tag):
                out = ctypes.c_void_p()
                wincom.call(self._factory, FACTORY_CREATE, "CreateSpellChecker", tag, ctypes.byref(out),
                            argtypes=(wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)))
                self._checker = out.value
                self.language = tag
                break
        if not self._checker:
            self.close()
            raise OSError("No Windows spelling dictionary is installed")
        self._cache: dict[str, bool] = {}

    def _supported(self, tag: str) -> bool:
        ok = wintypes.BOOL()
        try:
            wincom.call(self._factory, FACTORY_IS_SUPPORTED, "IsSupported", tag, ctypes.byref(ok),
                        argtypes=(wintypes.LPCWSTR, ctypes.POINTER(wintypes.BOOL)))
        except OSError:
            return False
        return bool(ok.value)

    @property
    def available(self) -> bool:
        return bool(self._checker)

    def check(self, word: str) -> bool:
        cached = self._cache.get(word)
        if cached is not None:
            return cached
        errors = ctypes.c_void_p()
        try:
            wincom.call(self._checker, CHECKER_CHECK, "Check", word, ctypes.byref(errors),
                        argtypes=(wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)))
        except OSError:
            return True
        correct = True
        try:
            while True:
                err = ctypes.c_void_p()
                hr = wincom.method(errors.value, ENUM_NEXT, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                    ctypes.byref(err))
                if hr != S_OK or not err.value:
                    break
                action = ctypes.c_int()
                wincom.method(err.value, ERROR_CORRECTIVE_ACTION, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                    ctypes.byref(action))
                wincom.release(err.value)
                if action.value in (1, 2):  # get suggestions / replace: a misspelling
                    correct = False
        finally:
            wincom.release(errors.value)
        if len(self._cache) > 20000:
            self._cache.clear()
        self._cache[word] = correct
        return correct

    def suggest(self, word: str, limit: int = 6) -> list[str]:
        enum = ctypes.c_void_p()
        try:
            wincom.call(self._checker, CHECKER_SUGGEST, "Suggest", word, ctypes.byref(enum),
                        argtypes=(wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)))
        except OSError:
            return []
        try:
            return _strings(enum.value, limit) if enum.value else []
        finally:
            wincom.release(enum.value)

    def learn(self, word: str):
        try:
            wincom.call(self._checker, CHECKER_ADD, "Add", word, argtypes=(wintypes.LPCWSTR,))
        except OSError:
            pass
        self._cache[word] = True

    def ignore(self, word: str):
        try:
            wincom.call(self._checker, CHECKER_IGNORE, "Ignore", word, argtypes=(wintypes.LPCWSTR,))
        except OSError:
            pass
        self._cache[word] = True

    def close(self):
        wincom.release(self._checker)
        wincom.release(self._factory)
        self._checker = self._factory = None


def user_language() -> str | None:
    buf = ctypes.create_unicode_buffer(85)
    if ctypes.WinDLL("kernel32").GetUserDefaultLocaleName(buf, len(buf)):
        return buf.value
    return None


def make_speller():
    try:
        return WindowsSpeller(user_language())
    except (OSError, AttributeError):
        return None
