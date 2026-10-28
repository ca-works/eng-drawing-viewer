import hashlib
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpus import make_corpus  # noqa: E402
from eng_viewer.core.netguard import install  # noqa: E402

install()


@pytest.fixture(scope="session")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("drawings")
    return make_corpus(root)


@pytest.fixture(scope="session")
def corpus_hashes(corpus):
    """Hash + mtime of every corpus file before any test touches it."""
    out = {}
    for p in Path(corpus["root"]).rglob("*"):
        if p.is_file():
            out[p] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return out


@pytest.fixture(scope="session")
def render(qapp):
    from eng_viewer.core.render_service import RenderService

    svc = RenderService(max_workers=3)
    yield svc
    svc.shutdown()


@pytest.fixture()
def settings(tmp_path):
    from eng_viewer.core.config import Settings

    return Settings(tmp_path / "settings.json")


@pytest.fixture(autouse=True, scope="session")
def _isolated_cache(tmp_path_factory):
    """Keep the thumbnail disk cache out of the user's real cache folder."""
    cache = tmp_path_factory.mktemp("cache")
    import eng_viewer.core.config as config
    import eng_viewer.ui.thumbnails as thumbnails

    config.cache_dir = lambda: cache
    thumbnails.cache_dir = lambda: cache
    yield


@pytest.fixture(scope="session")
def theme(qapp):
    from eng_viewer.ui.theme import ThemeManager

    tm = ThemeManager(qapp, "light")
    tm.apply()
    return tm


def wait_for(qtbot, predicate, timeout=20000):
    qtbot.waitUntil(predicate, timeout=timeout)


def run_job(qtbot, render, kind, args, **kw):
    """Submit a render job and block (processing events) until it finishes."""
    box = {}
    render.submit(kind, args, lambda res: box.setdefault("res", res), **kw)
    qtbot.waitUntil(lambda: "res" in box, timeout=30000)
    return box["res"]
