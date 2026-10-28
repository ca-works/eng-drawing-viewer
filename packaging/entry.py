"""Frozen-app entry point (PyInstaller).

``freeze_support()`` must run before anything else: render workers are spawned
by re-launching this executable, and they must not import Qt.
"""
import multiprocessing

if __name__ == "__main__":
    from eng_viewer.core.netguard import install

    install()
    multiprocessing.freeze_support()
    from eng_viewer.app import main

    raise SystemExit(main())
