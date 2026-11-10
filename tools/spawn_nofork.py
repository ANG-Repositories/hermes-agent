"""Popen without fork_exec where fork copies the parent (t_afc525a2).

On macOS ``_posixsubprocess.fork_exec`` does a real fork() of the whole multi-GB
gateway with the GIL held. This module starts the child with ``posix_spawn`` instead.
posix_spawn can neither chdir nor (on macOS) close inherited descriptors, so the
spawned image is a fresh isolated interpreter that closes descriptors in its OWN
single-threaded fd table, chdirs, and execs the target with the environment
untouched. No shell sits between Popen and the target, so the target sees what plain
Popen would give it. On Linux fork_exec uses vfork and copies nothing: callers get
plain Popen there.
"""
from __future__ import annotations

import errno
import functools
import os
import signal
import subprocess
import sys
from typing import Any, Sequence

# Platforms where fork_exec copies the parent's address space under the GIL.
_FORK_COPIES_PARENT = sys.platform == "darwin"
_FORK_ONLY_KWARGS = frozenset({
    "preexec_fn", "pass_fds", "close_fds", "start_new_session", "process_group",
    "user", "group", "extra_groups", "umask", "shell", "executable", "startupinfo",
    "creationflags", "restore_signals",
})
_RESET_SIGNALS = tuple(s for s in (getattr(signal, n, None) for n in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"))
                       if s is not None)
# argv after -c: the caller's LC_CTYPE ("-" = unset, "=v" = v; interpreter start may
# coerce it, PEP 538), cwd ("" = none), then the target argv. -I -S: ignore PYTHON*
# variables and site/user startup code; they stay in the environment for the target.
# Failure before exec exits 126 (cwd) or 127 (exec) with a message on stderr.
_EXEC_HELPER = r"""
import os, signal, sys
lc, cwd, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), signal.SIG_DFL)
if lc == "-":
    os.environ.pop("LC_CTYPE", None)
else:
    os.environ["LC_CTYPE"] = lc[1:]
fds = [int(n) for n in os.listdir("/proc/self/fd" if sys.platform.startswith("linux") else "/dev/fd")]
os.closerange(3, max(fds + [2]) + 1)
if cwd:
    try:
        os.chdir(cwd)
    except OSError:
        os.write(2, b"hermes: cannot enter working directory: " + os.fsencode(cwd) + b"\n")
        os._exit(126)
try:
    os.execv(argv[0], argv)
except OSError as exc:
    os.write(2, os.fsencode(f"hermes: cannot execute {argv[0]}: {exc.strerror}\n"))
    os._exit(127)
"""


def eligible(args: Any, kwargs: dict) -> bool:
    if not _FORK_COPIES_PARENT or os.name != "posix" or not hasattr(os, "posix_spawn"):
        return False
    if os.environ.get("HERMES_SPAWN_LEGACY", "").strip() not in ("", "0"):
        return False
    if not isinstance(subprocess.Popen, type) or _FORK_ONLY_KWARGS.intersection(kwargs):
        return False
    return (not isinstance(args, (str, bytes, os.PathLike)) and bool(args)
            and bool(os.path.dirname(os.fsdecode(args[0]))))


def _check_executable(path, cwd) -> None:
    """Raise from the constructor what Popen raises for a target it cannot exec."""
    target = os.path.join(os.fsdecode(cwd), os.fsdecode(path)) if cwd is not None else os.fsdecode(path)
    if not os.path.exists(target):
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
    if os.path.isdir(target) or not os.access(target, os.X_OK):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)


def _lc_ctype_marker(env) -> str:
    for key, value in env.items():
        if os.fsencode(key) == b"LC_CTYPE":
            return "=" + os.fsdecode(value)
    return "-"


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
            if len(args) > len(parameters):
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
            argv = [os.fsdecode(a) for a in bound["args"]]
            cwd = bound["cwd"]
            env = bound["env"] if bound["env"] is not None else os.environ
            _check_executable(argv[0], cwd)
            actions = []
            for fd in (p2cwrite, c2pread, errread):
                if fd != -1:
                    actions.append((os.POSIX_SPAWN_CLOSE, fd))
            for fd, target in ((p2cread, 0), (c2pwrite, 1), (errwrite, 2)):
                if fd != -1:
                    actions.append((os.POSIX_SPAWN_DUP2, fd, target))
            helper = [sys.executable, "-I", "-S", "-c", _EXEC_HELPER, _lc_ctype_marker(env),
                      os.fsdecode(cwd) if cwd is not None else "", *argv]
            spawn_kwargs = {"file_actions": actions, "setsigdef": list(_RESET_SIGNALS)}
            if bound["start_new_session"]:
                # Do not silently weaken session isolation to just a process group.
                spawn_kwargs["setsid"] = True
            try:
                self.pid = os.posix_spawn(helper[0], helper, env, **spawn_kwargs)
            except NotImplementedError:
                return super()._execute_child(*args, **kwargs)
            self._child_created = True
            self._close_pipe_fds(*handles)

    return NoForkPopen


def spawn_popen(args: Sequence[str], *, new_session: bool = False, **kwargs) -> subprocess.Popen:
    """Popen(close_fds=True[, start_new_session]); no fork_exec where fork copies the parent."""
    cls = _popen_class(subprocess.Popen) if eligible(args, kwargs) else subprocess.Popen
    return cls(args, start_new_session=new_session, **kwargs)


def run_nofork(args: Sequence[str], *, input: bytes | str | None = None, timeout: float | None = None,
               **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run's input/capture/check/timeout contract, using spawn_popen."""
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
