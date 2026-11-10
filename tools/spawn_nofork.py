"""Popen pipe handling with posix_spawn, session isolation and closed inherited fds.

The terminal tool avoids cwd= by changing directory in its shell trampoline. On
macOS, avoiding fork_exec avoids copying the multi-GB gateway under its GIL.
Unsupported Popen options retain ordinary Popen semantics (including close_fds).
"""
from __future__ import annotations

import functools
import os
import signal
import subprocess
import sys
from typing import Any, Sequence

_FORK_ONLY_KWARGS = frozenset({
    "cwd", "preexec_fn", "pass_fds", "close_fds", "start_new_session", "process_group",
    "user", "group", "extra_groups", "umask", "shell", "executable", "startupinfo",
    "creationflags", "restore_signals",
})
_HAVE_CLOSEFROM = hasattr(os, "POSIX_SPAWN_CLOSEFROM")
# Without CLOSEFROM, a fresh, isolated interpreter closes descriptors in its OWN
# single-threaded fd table before exec. Parent-side enumeration races other threads.
# -I -S ignores PYTHON* and site/user startup code. Failure to list/close aborts exec.
_CLOSE_EXEC = (
    "import os,sys,signal; "
    "restore=sys.argv.pop(1)=='1'; "
    "[signal.signal(s,signal.SIG_DFL) for s in (getattr(signal,n,None) for n in ('SIGPIPE','SIGXFZ','SIGXFSZ')) if restore and s is not None]; "
    "fds=[int(n) for n in os.listdir('/proc/self/fd' if sys.platform.startswith('linux') else '/dev/fd')]; "
    "os.closerange(3,max(fds+[2])+1); os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
)


def eligible(args: Any, kwargs: dict) -> bool:
    if os.name != "posix" or not hasattr(os, "posix_spawn"):
        return False
    if os.environ.get("HERMES_SPAWN_LEGACY", "").strip() not in ("", "0"):
        return False
    if not isinstance(subprocess.Popen, type) or _FORK_ONLY_KWARGS.intersection(kwargs):
        return False
    return (not isinstance(args, (str, bytes, os.PathLike)) and bool(args)
            and bool(os.path.dirname(os.fsdecode(args[0]))))


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
            argv = list(bound["args"])
            env = bound["env"] if bound["env"] is not None else os.environ
            actions = []
            for fd in (p2cwrite, c2pread, errread):
                if fd != -1:
                    actions.append((os.POSIX_SPAWN_CLOSE, fd))
            for fd, target in ((p2cread, 0), (c2pwrite, 1), (errwrite, 2)):
                if fd != -1:
                    actions.append((os.POSIX_SPAWN_DUP2, fd, target))
            if _HAVE_CLOSEFROM:
                actions.append((os.POSIX_SPAWN_CLOSEFROM, 3))
            else:
                argv = [sys.executable, "-I", "-S", "-c", _CLOSE_EXEC,
                        "1" if bound["restore_signals"] else "0", *argv]
            spawn_kwargs = {"file_actions": actions}
            if bound["restore_signals"]:
                spawn_kwargs["setsigdef"] = [
                    s for s in (getattr(signal, n, None) for n in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"))
                    if s is not None]
            if bound["start_new_session"]:
                # Do not silently weaken session isolation to just a process group.
                spawn_kwargs["setsid"] = True
            try:
                self.pid = os.posix_spawn(argv[0], argv, env, **spawn_kwargs)
            except NotImplementedError:
                return super()._execute_child(*args, **kwargs)
            self._child_created = True
            self._close_pipe_fds(*handles)

    return NoForkPopen


def spawn_popen(args: Sequence[str], *, new_session: bool = False, **kwargs) -> subprocess.Popen:
    """Popen with close_fds and optional new session, without forking when supported."""
    cls = _popen_class(subprocess.Popen) if eligible(args, kwargs) else subprocess.Popen
    return cls(args, start_new_session=new_session, **kwargs)


def run_nofork(args: Sequence[str], *, input: bytes | str | None = None, timeout: float | None = None,
               **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run's input/capture/check contract, using spawn_popen."""
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
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            stdout, stderr = proc.communicate()
            if not proc.text_mode:
                exc.stdout, exc.stderr = stdout, stderr
            raise
        except BaseException:
            proc.kill()
            raise
        result = subprocess.CompletedProcess(args, proc.poll(), stdout, stderr)
        if check:
            result.check_returncode()
        return result
