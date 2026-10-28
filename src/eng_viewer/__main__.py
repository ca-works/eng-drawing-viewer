# Kept import-free at module level: render workers are spawned processes that
# re-import ``__main__``, and they must not pull in Qt.
if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    from eng_viewer.app import main

    main()
