"""Spawn POSIX children through ``posix_spawn`` instead of ``fork`` (t_afc525a2).

``subprocess.Popen`` falls back to ``_posixsubprocess.fork_exec`` whenever a call
passes ``cwd=``, ``start_new_session=True``, ``process_group=`` or (before 3.13)
the default ``close_fds=True``. On macOS that is a real ``fork()`` of the
caller, taken with the GIL held. In a 6 GB, 200-thread gateway each terminal-tool
spawn then stalls every other Python thread for the time the kernel copies the
page tables (29% of the gateway's GIL samples, 2026-11-09).

``posix_spawn`` is a syscall on macOS and a ``CLONE_VM|CLONE_VFORK`` on glibc,
and its cost does not grow with the parent's RSS. ``os.posix_spawn`` can also
start a new session (``setsid``) or process group (``setpgroup``), which
``Popen`` never asks it to. So :func:`spawn_popen` keeps ``Popen``'s own
posix_spawn branch (no ``cwd``, ``close_fds=False``, no ``start_new_session``)
and overrides only ``_posix_spawn`` to add:

* ``setsid=True`` (or ``setpgroup=0`` where the platform lacks
  ``POSIX_SPAWN_SETSID``) when ``new_session`` is asked for;
* close actions for every inheritable fd >= 3, which is what ``close_fds=True``
  did (``POSIX_SPAWN_CLOSEFROM`` where Python has it).

``cwd`` is not supported: ``os.posix_spawn`` has no chdir action before 3.14.
Callers ``cd`` inside the child instead (see ``LocalEnvironment._run_bash``).

Anything the override cannot cover takes plain ``subprocess.Popen`` with the
caller's arguments, so semantics never depend on which path ran. Set
``HERMES_SPAWN_LEGACY=1`` to force that path everywhere.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Popen keyword arguments that force (or change) CPython's fork_exec branch; a call
# carrying any of them goes to plain Popen.
_FORK_ONLY_KWARGS = frozenset({
    "cwd", "preexec_fn", "pass_fds", "close_fds", "start_new_session", "process_group",
    "user", "group", "extra_groups", "umask", "shell", "executable", "startupinfo",
    "creationflags",
})
_FD_DIRS = ("/proc/self/fd", "/dev/fd")
_HAVE_CLOSEFROM = hasattr(os, "POSIX_SPAWN_CLOSEFROM")
# "setsid" until the platform refuses it once, then "setpgroup" for the process lifetime.
_session_mode = "setsid"
_warned_fallback = False


def _legacy_forced() -> bool:
    return os.environ.get("HERMES_SPAWN_LEGACY", "").strip() not in ("", "0")


def _std_fds_open() -> bool:
    """Popen's posix_spawn branch needs every pipe end above 2. New pipes land on
    the lowest free fd, so a closed 0/1/2 would send Popen to fork_exec."""
    for fd in (0, 1, 2):
        try:
            os.fstat(fd)
        except OSError:
            return False
    return True


def _inheritable_fds() -> list[int] | None:
    """Every fd >= 3 without FD_CLOEXEC, or None when the fd table cannot be listed."""
    for fd_dir in _FD_DIRS:
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        found = []
        for name in names:
            try:
                fd = int(name)
            except ValueError:
                continue
            if fd < 3:
                continue
            try:
                if os.get_inheritable(fd):
                    found.append(fd)
            except OSError:  # the listdir handle itself, or closed since
                continue
        return sorted(found)
    return None


def eligible(args: Any, kwargs: dict) -> bool:
    """True when :class:`_NoForkPopen` is guaranteed to reach its ``_posix_spawn``."""
    if os.name != "posix" or not getattr(subprocess, "_USE_POSIX_SPAWN", False):
        return False
    if _legacy_forced() or _FORK_ONLY_KWARGS.intersection(kwargs):
        return False
    if isinstance(args, (str, bytes, os.PathLike)) or not args:
        return False
    if not os.path.dirname(os.fsdecode(args[0])):  # Popen requires a path, not a bare name
        return False
    if not _std_fds_open():
        return False
    return _HAVE_CLOSEFROM or _inheritable_fds() is not None


def _posix_spawn_session(executable, args, env, kwargs: dict, new_session: bool) -> int:
    global _session_mode
    if not new_session:
        return os.posix_spawn(executable, args, env, **kwargs)
    if _session_mode == "setsid":
        try:
            return os.posix_spawn(executable, args, env, setsid=True, **kwargs)
        except NotImplementedError:
            _session_mode = "setpgroup"
            logger.info("posix_spawn: no POSIX_SPAWN_SETSID here; using setpgroup=0")
    # A new process group is what the killpg teardown needs; only the controlling-tty
    # detach is lost, and a daemonised gateway has no tty.
    return os.posix_spawn(executable, args, env, setpgroup=0, **kwargs)


class _NoForkPopen(subprocess.Popen):
    """``Popen`` whose posix_spawn branch also sets the session and closes inherited fds."""

    def __init__(self, args, *, new_session: bool = False, **kwargs):
        self._hermes_new_session = new_session
        self._hermes_posix_spawned = False
        super().__init__(args, close_fds=False, **kwargs)

    def _posix_spawn(self, args, executable, env, restore_signals, *handles_and_flags):
        # <=3.12: (p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)
        # >=3.13: (close_fds, p2cread, ...); close_fds is always False here.
        p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite = handles_and_flags[-6:]
        if env is None:
            env = os.environ
        kwargs: dict = {}
        if restore_signals:  # same set as CPython's _posix_spawn / _Py_RestoreSignals
            kwargs["setsigdef"] = [
                signum for signum in (getattr(signal, name, None) for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"))
                if signum is not None]
        file_actions: list = []
        if not _HAVE_CLOSEFROM:
            # close_fds=True by hand. Open /dev/null onto the fd first so the close cannot
            # fail with EBADF if another thread closed it after the listing (macOS fails
            # the whole spawn on a bad close action).
            handles = {p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite}
            for fd in _inheritable_fds() or ():
                if fd not in handles:
                    file_actions.append((os.POSIX_SPAWN_OPEN, fd, os.devnull, os.O_RDONLY, 0))
                    file_actions.append((os.POSIX_SPAWN_CLOSE, fd))
        for fd in (p2cwrite, c2pread, errread):
            if fd != -1:
                file_actions.append((os.POSIX_SPAWN_CLOSE, fd))
        for fd, target in ((p2cread, 0), (c2pwrite, 1), (errwrite, 2)):
            if fd != -1:
                file_actions.append((os.POSIX_SPAWN_DUP2, fd, target))
        if _HAVE_CLOSEFROM:
            file_actions.append((os.POSIX_SPAWN_CLOSEFROM, 3))
        if file_actions:
            kwargs["file_actions"] = file_actions
        self.pid = _posix_spawn_session(executable, args, env, kwargs, self._hermes_new_session)
        self._child_created = True
        self._hermes_posix_spawned = True
        self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)


def spawn_popen(args: Sequence[str], *, new_session: bool = False, **kwargs) -> subprocess.Popen:
    """``subprocess.Popen(args, start_new_session=new_session, **kwargs)`` without forking.

    Same semantics either way: fds >= 3 are not inherited, and with ``new_session``
    the child leads its own process group. Pass no ``cwd``; ``cd`` in the child.
    """
    global _warned_fallback
    if not eligible(args, kwargs):
        return subprocess.Popen(args, start_new_session=new_session, **kwargs)
    proc = _NoForkPopen(args, new_session=new_session, **kwargs)
    if not proc._hermes_posix_spawned and not _warned_fallback:
        # eligible() mirrors CPython's branch test; reaching here means it drifted (a new
        # Python). The child ran with inherited fds and no new session: say so loudly.
        _warned_fallback = True
        logger.warning("spawn_popen: CPython took fork_exec for %r; eligible() is out of date", args[0])
    return proc


def run_nofork(args: Sequence[str], *, input: bytes | str | None = None, timeout: float | None = None,
               **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` built on :func:`spawn_popen` (no ``check``; kill on timeout)."""
    with spawn_popen(args, **kwargs) as proc:
        try:
            stdout, stderr = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            exc.stdout, exc.stderr = proc.communicate()
            raise
        except BaseException:
            proc.kill()
            raise
    return subprocess.CompletedProcess(args, proc.poll(), stdout, stderr)
