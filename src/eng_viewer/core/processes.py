"""Bounded external conversion jobs. No shell; terminate contained descendants."""
from __future__ import annotations

import math
import os
import signal
import subprocess


def run_conversion(command: list[str], timeout: float = 300) -> int:
    """Run a converter in a new POSIX session or Windows kill-on-close Job.

    Discard converter output: malformed files must not flood parent memory or
    leave document text in logs. This limits process lifetime, not file access
    or networking, and cannot contain POSIX children that deliberately detach.
    """
    if not math.isfinite(timeout) or not 0 < timeout < 86400:
        raise ValueError("Invalid conversion timeout")
    if not command or not os.path.isabs(command[0]):
        raise ValueError("Converter executable must be an absolute path")
    if os.name == "nt":
        return _run_windows(command, timeout)
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return proc.wait(timeout=timeout)
    finally:
        # Also retire any ordinary descendants left after the launcher exits.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            proc.wait(timeout=10)


def _run_windows(command: list[str], timeout: float) -> int:
    # The Windows distribution already depends on pywin32. Fail before launch
    # if job containment is unavailable; never silently use parent-only kill.
    import win32api
    import win32con
    import win32event
    import win32job
    import win32process

    job = win32job.CreateJobObject(None, None)
    process = thread = None
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        # Assign before executing user code, so children cannot escape the
        # launch/assignment race. Do not enable either breakaway flag.
        process, thread, _, _ = win32process.CreateProcess(
            command[0], subprocess.list2cmdline(command), None, None, False,
            win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW,
            None, None, win32process.STARTUPINFO())
        win32job.AssignProcessToJobObject(job, process)
        win32process.ResumeThread(thread)
        status = win32event.WaitForSingleObject(process, math.ceil(timeout * 1000))
        if status == win32event.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(command, timeout)
        if status != win32event.WAIT_OBJECT_0:
            raise OSError("Could not wait for document conversion")
        return win32process.GetExitCodeProcess(process)
    finally:
        try:
            if process is not None:
                # Also works if assignment failed while the child was suspended.
                try:
                    win32job.TerminateJobObject(job, 1)
                finally:
                    if win32process.GetExitCodeProcess(process) == win32con.STILL_ACTIVE:
                        win32process.TerminateProcess(process, 1)
                    win32event.WaitForSingleObject(process, 10000)
        finally:
            for handle in (thread, process, job):
                if handle is not None:
                    win32api.CloseHandle(handle)
