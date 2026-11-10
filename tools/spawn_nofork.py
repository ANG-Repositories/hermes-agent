"""Popen without fork_exec where fork copies the parent (t_afc525a2).

On macOS ``_posixsubprocess.fork_exec`` does a real fork() of the whole multi-GB
gateway with the GIL held. There this module starts the child with ONE libc
``posix_spawn`` call through ctypes (which also releases the GIL for the call).
Apple's extensions do in the kernel what ``Popen(close_fds=True, cwd=,
start_new_session=)`` does in its forked child: POSIX_SPAWN_CLOEXEC_DEFAULT closes
every descriptor no file action names, ``addinherit_np`` keeps the parent's 0/1/2,
``adddup2`` places the pipe ends, ``addchdir`` enters the cwd, POSIX_SPAWN_SETSID
starts the session and POSIX_SPAWN_SETSIGDEF resets what ``restore_signals`` resets.
Nothing runs between the spawn call and the target: it gets exactly the argv and
environment it was given, and launch errors come back from the call as errno.

Off macOS fork_exec uses vfork and copies nothing: callers get plain Popen there. A
missing binding or a failed attribute setup means plain Popen for the rest of the
process (logged once).
"""
from __future__ import annotations

import ctypes
import functools
import logging
import os
import signal
import subprocess
import sys
import threading
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Platforms where fork_exec copies the parent's address space under the GIL.
_FORK_COPIES_PARENT = sys.platform == "darwin"
_FORK_ONLY_KWARGS = frozenset({
    "preexec_fn", "pass_fds", "close_fds", "start_new_session", "process_group",
    "user", "group", "extra_groups", "umask", "shell", "executable", "startupinfo",
    "creationflags", "restore_signals",
})
# <sys/spawn.h>, macOS SDK 26. posix_spawnattr_setflags takes a short.
_POSIX_SPAWN_SETSIGDEF = 0x0004
_POSIX_SPAWN_SETSID = 0x0400
_POSIX_SPAWN_CLOEXEC_DEFAULT = 0x4000
# What Popen(restore_signals=True) resets to SIG_DFL in the child.
_RESET_SIGNALS = tuple(s for s in (getattr(signal, n, None) for n in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"))
                       if s is not None)


class _SetupFailed(Exception):
    """An attribute or file-action call failed before posix_spawn itself ran."""


class _Libc:
    """libSystem's posix_spawn family; signatures from <spawn.h> (macOS SDK 26)."""

    def __init__(self, libc):
        handle = ctypes.c_void_p  # posix_spawnattr_t / posix_spawn_file_actions_t: void *
        ptr = ctypes.POINTER

        def fn(name, *argtypes):
            f = getattr(libc, name)
            f.argtypes, f.restype = list(argtypes), ctypes.c_int
            return f

        self.handle = handle
        self.attr_init = fn("posix_spawnattr_init", ptr(handle))
        self.attr_destroy = fn("posix_spawnattr_destroy", ptr(handle))
        self.attr_setflags = fn("posix_spawnattr_setflags", ptr(handle), ctypes.c_short)
        # sigset_t is __uint32_t on Darwin.
        self.attr_setsigdefault = fn("posix_spawnattr_setsigdefault", ptr(handle), ptr(ctypes.c_uint32))
        self.fa_init = fn("posix_spawn_file_actions_init", ptr(handle))
        self.fa_destroy = fn("posix_spawn_file_actions_destroy", ptr(handle))
        self.fa_dup2 = fn("posix_spawn_file_actions_adddup2", ptr(handle), ctypes.c_int, ctypes.c_int)
        self.fa_inherit = fn("posix_spawn_file_actions_addinherit_np", ptr(handle), ctypes.c_int)
        try:  # macOS 26 name; the _np spelling is deprecated there
            self.fa_chdir = fn("posix_spawn_file_actions_addchdir", ptr(handle), ctypes.c_char_p)
        except AttributeError:
            self.fa_chdir = fn("posix_spawn_file_actions_addchdir_np", ptr(handle), ctypes.c_char_p)
        self.spawn = fn("posix_spawn", ptr(ctypes.c_int32), ctypes.c_char_p, ptr(handle), ptr(handle),
                        ptr(ctypes.c_char_p), ptr(ctypes.c_char_p))


def _load_libc():
    return ctypes.CDLL(None)


_UNRESOLVED = object()
_libc: Any = _UNRESOLVED  # a _Libc, or None once unavailable/disabled
_libc_lock = threading.Lock()


def _bindings() -> "_Libc | None":
    global _libc
    if _libc is _UNRESOLVED:
        with _libc_lock:
            if _libc is _UNRESOLVED:
                try:
                    _libc = _Libc(_load_libc())
                except Exception as exc:  # noqa: BLE001 - any binding problem means plain Popen
                    _libc = None
                    logger.warning("spawn_nofork: libc posix_spawn bindings unavailable (%s); using Popen", exc)
    return _libc


def _disable(reason: str) -> None:
    global _libc
    with _libc_lock:
        if _libc is not None:
            _libc = None
            logger.warning("spawn_nofork: %s; using Popen for the rest of this process", reason)


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _native_spawn(libc: _Libc, executable: bytes, argv: list, envp: list, cwd: "bytes | None",
                  new_session: bool, dup2s: list) -> tuple:
    """One posix_spawn call -> (return code, pid). _SetupFailed if setup fails before it."""
    attr, actions, pid = libc.handle(), libc.handle(), ctypes.c_int32(0)
    if libc.attr_init(ctypes.byref(attr)) != 0:
        raise _SetupFailed("posix_spawnattr_init")
    try:
        if libc.fa_init(ctypes.byref(actions)) != 0:
            raise _SetupFailed("posix_spawn_file_actions_init")
        try:
            sigs = ctypes.c_uint32(0)
            for sig in _RESET_SIGNALS:
                sigs.value |= 1 << (sig - 1)
            flags = _POSIX_SPAWN_SETSIGDEF | _POSIX_SPAWN_CLOEXEC_DEFAULT
            if new_session:
                flags |= _POSIX_SPAWN_SETSID
            steps = [("setsigdefault", libc.attr_setsigdefault(ctypes.byref(attr), ctypes.byref(sigs))),
                     ("setflags", libc.attr_setflags(ctypes.byref(attr), flags))]
            for fd, target in dup2s:
                steps.append(("adddup2", libc.fa_dup2(ctypes.byref(actions), fd, target)))
            redirected = {target for _, target in dup2s}
            for fd in (0, 1, 2):  # Popen leaves an unredirected, open standard fd to the child
                if fd not in redirected and _is_open(fd):
                    steps.append(("addinherit_np", libc.fa_inherit(ctypes.byref(actions), fd)))
            if cwd is not None:
                steps.append(("addchdir", libc.fa_chdir(ctypes.byref(actions), cwd)))
            failed = [name for name, rc in steps if rc != 0]
            if failed:
                raise _SetupFailed(", ".join(failed))
            c_argv = (ctypes.c_char_p * (len(argv) + 1))(*argv, None)
            c_envp = (ctypes.c_char_p * (len(envp) + 1))(*envp, None)
            rc = libc.spawn(ctypes.byref(pid), executable, ctypes.byref(actions), ctypes.byref(attr),
                            c_argv, c_envp)
            return rc, pid.value
        finally:
            libc.fa_destroy(ctypes.byref(actions))
    finally:
        libc.attr_destroy(ctypes.byref(attr))


def _encode(value, what: str) -> bytes:
    data = os.fsencode(value)
    if b"\0" in data:
        raise ValueError(f"embedded null byte in {what}")
    return data


def eligible(args: Any, kwargs: dict) -> bool:
    if not _FORK_COPIES_PARENT or os.name != "posix":
        return False
    if os.environ.get("HERMES_SPAWN_LEGACY", "").strip() not in ("", "0"):
        return False
    if not isinstance(subprocess.Popen, type) or _FORK_ONLY_KWARGS.intersection(kwargs):
        return False
    # posix_spawn does no PATH search, and a relative path with a cwd is resolved
    # against different directories by Popen and posix_spawn: absolute only.
    if isinstance(args, (str, bytes, os.PathLike)) or not args or not os.path.isabs(os.fsdecode(args[0])):
        return False
    return _bindings() is not None


@functools.lru_cache(maxsize=4)
def _popen_class(base):
    # Lazy: tools are also imported with Popen replaced by a recording function.
    # Popen.__init__ supplies the POSIX ABI positionally. Instrumentation may
    # wrap _execute_child with *rest/**kw, hiding its signature; never bind to
    # that wrapper. Keep its original argv for the ordinary-Popen fallback.
    parameters = (
        "args", "executable", "preexec_fn", "close_fds", "pass_fds", "cwd", "env",
        "startupinfo", "creationflags", "shell", "p2cread", "p2cwrite", "c2pread",
        "c2pwrite", "errread", "errwrite", "restore_signals", "gid", "gids", "uid",
        "umask", "start_new_session", "process_group",
    )

    class NoForkPopen(base):
        def _execute_child(self, *args, **kwargs):
            libc = _bindings()
            if libc is None or len(args) > len(parameters):
                return super()._execute_child(*args, **kwargs)
            bound = dict(zip(parameters, args))
            bound.update(kwargs)
            handles = [bound[n] for n in
                       ("p2cread", "p2cwrite", "c2pread", "c2pwrite", "errread", "errwrite")]
            p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite = handles
            # Pipes can occupy 0/1/2 when standard descriptors are closed. Let
            # CPython handle those dup-order corner cases with its original flags.
            if any(0 <= fd <= 2 for fd in handles):
                return super()._execute_child(*args, **kwargs)
            argv_in, cwd, env = list(bound["args"]), bound["cwd"], bound["env"]
            sys.audit("subprocess.Popen", argv_in[0], argv_in, cwd, env)
            argv = [_encode(a, "argument") for a in argv_in]
            if env is None:
                envp = [k + b"=" + v for k, v in os.environb.items()]
            else:
                envp = []
                for key, value in env.items():
                    key = _encode(key, "environment variable name")
                    if b"=" in key:
                        raise ValueError("illegal environment variable name")
                    envp.append(key + b"=" + _encode(value, "environment variable"))
            c_cwd = _encode(cwd, "cwd") if cwd is not None else None
            dup2s = [(fd, target) for fd, target in ((p2cread, 0), (c2pwrite, 1), (errwrite, 2)) if fd != -1]
            try:
                rc, pid = _native_spawn(libc, argv[0], argv, envp, c_cwd,
                                        bool(bound["start_new_session"]), dup2s)
            except _SetupFailed as exc:
                _disable(f"posix_spawn setup failed ({exc})")
                return super()._execute_child(*args, **kwargs)
            if rc != 0:
                # As Popen: the cwd is named when the child could not enter it, else the executable.
                enterable = cwd is None or (os.path.isdir(cwd) and os.access(cwd, os.X_OK))
                raise OSError(rc, os.strerror(rc), argv_in[0] if enterable else cwd)
            self.pid = pid
            self._child_created = True
            self._close_pipe_fds(*handles)

    return NoForkPopen


def spawn_popen(args: Sequence[str], *, new_session: bool = False, **kwargs) -> subprocess.Popen:
    """Popen(close_fds=True[, start_new_session]); no fork_exec where fork copies the parent."""
    cls = _popen_class(subprocess.Popen) if eligible(args, kwargs) else subprocess.Popen
    return cls(args, start_new_session=new_session, **kwargs)


def run_nofork(args: Sequence[str], *, input: "bytes | str | None" = None, timeout: "float | None" = None,
               **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run through spawn_popen where eligible; subprocess.run itself otherwise."""
    if not eligible(args, kwargs):
        return subprocess.run(args, input=input, timeout=timeout, **kwargs)
    check = kwargs.pop("check", False)
    if input is not None:
        if "stdin" in kwargs:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if kwargs.pop("capture_output", False):
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with spawn_popen(args, **kwargs) as proc:
        try:
            stdout, stderr = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            # subprocess.run's POSIX path: communicate already put the output so far on
            # the exception. Communicating again would block until every descendant
            # holding the pipes exits.
            proc.kill()
            proc.wait()
            raise
        except BaseException:
            proc.kill()
            raise
        result = subprocess.CompletedProcess(args, proc.poll(), stdout, stderr)
        if check:
            result.check_returncode()
        return result
