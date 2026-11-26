"""Stall detection for the commands JARVIS runs: no fixed time limits.

A command runs while it shows activity: output, or a change in CPU time or
I/O bytes anywhere in its process tree (read from ``/proc``). One that shows
none for the stall window is stopped. :func:`run_watched` reports a running
command every :data:`PROGRESS_SECONDS` and retries a stalled one visibly;
:class:`ActivityWatch` is the same test for a caller that owns the process.

The window is ``JARVIS_STALL_SECONDS`` (``JARVIS_SPACK_STALL_SECONDS`` is
read too, the 1.9.19 name), default :data:`DEFAULT_STALL_SECONDS`, never
under :data:`MIN_STALL_SECONDS`. Without ``/proc`` (macOS, Windows) only
output counts; where a caller has no output either, nothing is a stall.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Deque, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

STALL_SECONDS_ENV = "JARVIS_STALL_SECONDS"
#: the 1.9.19 name, still read when :data:`STALL_SECONDS_ENV` is unset
LEGACY_STALL_SECONDS_ENV = "JARVIS_SPACK_STALL_SECONDS"
DEFAULT_STALL_SECONDS = 600
MIN_STALL_SECONDS = 30
#: how often a running command reports that it is still working
PROGRESS_SECONDS = 60
#: attempts of a command that stalls before the caller gets a stall result
STALL_ATTEMPTS = 2
POLL_SECONDS = 5.0
OUTPUT_TAIL_CHARS = 4096
#: the returncode of a command that stalled on every attempt
STALLED_EXIT_CODE = 124

Say = Callable[..., None]


def stall_seconds() -> int:
    """The stall window, in seconds."""
    raw = (os.environ.get(STALL_SECONDS_ENV, "").strip()
           or os.environ.get(LEGACY_STALL_SECONDS_ENV, "").strip())
    try:
        value = int(raw) if raw else DEFAULT_STALL_SECONDS
    except ValueError:
        value = DEFAULT_STALL_SECONDS
    return max(value, MIN_STALL_SECONDS)


def process_tree(root_pid: int) -> List[int]:
    """``root_pid`` and its live descendants (from ``/proc``)."""
    children: Dict[int, List[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return [root_pid]
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as stream:
                fields = stream.read().rsplit(b")", 1)[1].split()
        except (OSError, IndexError):
            continue
        children.setdefault(int(fields[1]), []).append(int(entry))
    tree, pending = [], [root_pid]
    while pending:
        pid = pending.pop()
        tree.append(pid)
        pending.extend(children.get(pid, ()))
    return tree


def _proc_activity(base: str, children: bool = True) -> tuple:
    """CPU ticks and I/O bytes of one ``/proc`` entry (a process or a task)."""
    cpu = io_bytes = 0
    try:
        with open(f"{base}/stat", "rb") as stream:
            fields = stream.read().rsplit(b")", 1)[1].split()
        cpu += int(fields[11]) + int(fields[12])
        if children:
            cpu += int(fields[13]) + int(fields[14])
    except (OSError, IndexError, ValueError):
        pass
    try:
        with open(f"{base}/io", "rb") as stream:
            for line in stream:
                if line.startswith((b"rchar:", b"wchar:")):
                    io_bytes += int(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    return cpu, io_bytes


def tree_activity(root_pid: int) -> Optional[Dict[str, int]]:
    """CPU ticks and I/O bytes of the process tree, or None without ``/proc``."""
    if not os.path.isdir("/proc/self"):
        return None
    cpu = io_bytes = 0
    pids = process_tree(root_pid)
    for pid in pids:
        pid_cpu, pid_io = _proc_activity(f"/proc/{pid}")
        cpu += pid_cpu
        io_bytes += pid_io
    return {"cpu_ticks": cpu, "io_bytes": io_bytes, "processes": len(pids)}


def stop_group(process: subprocess.Popen) -> None:
    """SIGTERM, then SIGKILL, the process group a watched command leads."""
    import signal

    killpg = getattr(os, "killpg", None)
    for sig, grace in ((signal.SIGTERM, 10), (getattr(signal, "SIGKILL", signal.SIGTERM), 10)):
        try:
            if killpg is None:
                raise OSError("no process groups")
            killpg(process.pid, sig)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


class ActivityWatch:
    """When a process tree last showed activity.

    ``sample(output_at)`` takes the time of the caller's last output line
    (``time.monotonic()``) and returns the seconds since any activity.
    ``watched`` is False without ``/proc``: then only output counts.
    """

    def __init__(self, pid: int):
        self.pid = pid
        self.started = self.last_active = time.monotonic()
        self.activity = tree_activity(pid)
        self.watched = self.activity is not None

    def sample(self, output_at: Optional[float] = None) -> float:
        now = time.monotonic()
        if output_at is not None and output_at > self.last_active:
            self.last_active = output_at
        current = tree_activity(self.pid)
        if current is not None and self.activity is not None and (
                current["cpu_ticks"] != self.activity["cpu_ticks"]
                or current["io_bytes"] != self.activity["io_bytes"]):
            self.last_active = now
        if current is not None:
            self.activity = current
        return now - self.last_active

    def describe(self) -> str:
        if self.activity is None:
            return ""
        return (f"{self.activity['processes']} process(es), cpu "
                f"{self.activity['cpu_ticks']} ticks, io {self.activity['io_bytes']} bytes; ")


class OutputSink:
    """Output lines that commands print for a watcher that doesn't own them.

    A thread sets one with :func:`collecting`; the commands it starts
    (:class:`jarvis_cd.shell.core_exec.CoreExec`, :func:`run_watched`) note
    each line they read into it, from their own reader threads.
    """

    def __init__(self, tail_lines: int = 40):
        self._lock = threading.Lock()
        self.last_at: Optional[float] = None
        self.lines = 0
        self._tail: Deque[str] = deque(maxlen=tail_lines)

    def note(self, line: str) -> None:
        with self._lock:
            self.last_at = time.monotonic()
            self.lines += 1
            self._tail.append(line if line.endswith("\n") else line + "\n")

    def tail(self) -> str:
        with self._lock:
            return "".join(self._tail)[-OUTPUT_TAIL_CHARS:]


_sink_local = threading.local()


def current_sink() -> Optional[OutputSink]:
    """The :class:`OutputSink` the calling thread collects into, if any."""
    return getattr(_sink_local, "sink", None)


@contextmanager
def collecting(sink: OutputSink) -> Iterator[OutputSink]:
    """Note the output of commands this thread starts into ``sink``."""
    previous = current_sink()
    _sink_local.sink = sink
    try:
        yield sink
    finally:
        _sink_local.sink = previous


class StartWatch:
    """When work a thread of this process does last showed activity.

    For a caller that waits on a thread rather than a command (a background
    service's ``start()``). Activity is any of: the thread's own CPU time or
    I/O, a process this process started since the watch began (appearing,
    exiting, or using CPU time or I/O, so ``ssh``/``mpiexec`` reading a
    remote command's output counts), an output line in ``sink``, or a change
    to one of ``paths`` (size or mtime). Processes that existed when the
    watch began, and this process's other threads, don't count.
    ``watched`` is False without ``/proc``: then only output and ``paths``
    count, and a caller should not call anything a stall.
    """

    def __init__(self, sink: Optional[OutputSink] = None, paths: Iterable[str] = ()):
        self.sink = sink
        self.paths = [str(path) for path in paths if path]
        self.thread_id: Optional[int] = None
        self.watched = os.path.isdir("/proc/self")
        self._baseline = set(process_tree(os.getpid())) if self.watched else set()
        self.started = self.last_active = time.monotonic()
        self.activity = self._read()

    def _files(self) -> tuple:
        state = []
        for path in self.paths:
            try:
                info = os.stat(path)
                state.append((info.st_mtime_ns, info.st_size))
            except OSError:
                state.append(None)
        return tuple(state)

    def _read(self) -> Dict[str, Any]:
        reading: Dict[str, Any] = {"files": self._files()}
        if not self.watched:
            return reading
        cpu = io_bytes = 0
        if self.thread_id is not None:
            cpu, io_bytes = _proc_activity(f"/proc/self/task/{self.thread_id}",
                                           children=False)
        new = sorted(pid for pid in process_tree(os.getpid()) if pid not in self._baseline)
        for pid in new:
            pid_cpu, pid_io = _proc_activity(f"/proc/{pid}")
            cpu += pid_cpu
            io_bytes += pid_io
        reading.update(cpu_ticks=cpu, io_bytes=io_bytes, processes=len(new),
                       pids=tuple(new))
        return reading

    def sample(self) -> float:
        """Seconds since the last activity."""
        now = time.monotonic()
        output_at = self.sink.last_at if self.sink is not None else None
        if output_at is not None and output_at > self.last_active:
            self.last_active = output_at
        current = self._read()
        if current != self.activity:
            self.last_active = now
        self.activity = current
        return now - self.last_active

    def describe(self) -> str:
        lines = self.sink.lines if self.sink is not None else 0
        text = f"{lines} output line(s)"
        if self.watched:
            text += (f", {self.activity['processes']} process(es), cpu "
                     f"{self.activity['cpu_ticks']} ticks, io {self.activity['io_bytes']} bytes")
        return text + "; "

    def record(self) -> Dict[str, Any]:
        """The current activity, for a stall report."""
        return {key: value for key, value in self.activity.items()
                if key in ("cpu_ticks", "io_bytes", "processes")}


def _stderr_say(text: str, *, record: bool = True) -> None:
    try:
        sys.stderr.write(f"JARVIS: {text}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def run_watched(command: Sequence[str], environment: Optional[Mapping[str, str]] = None,
                say: Optional[Say] = None, *, what: Optional[str] = None,
                cwd: Optional[str] = None, window: Optional[int] = None,
                attempts: int = STALL_ATTEMPTS, merge_stderr: bool = False,
                echo: Optional[bool] = None) -> subprocess.CompletedProcess:
    """Run ``command`` to the end, however long, unless it stalls.

    ``say(text, record=...)`` gets the command line, its exit and every
    stall and retry (``record=True``) and the "still running" lines
    (``record=False``). Without ``say`` only the progress, stall and retry
    lines are written, to stderr; ``echo=False`` leaves the command line and
    its exit out of ``say`` (a caller that logs them itself). A stalled command is stopped and run
    again, up to ``attempts`` times; one that stalled every time returns
    :data:`STALLED_EXIT_CODE` with its output so far and a ``stall`` record
    ``{stalls: [{attempt, after_s, idle_s, activity, output_tail}],
    recovered, window_s, attempts}``. A command that could not start
    returns 127 with the error as stderr.
    """
    announce = (say is not None) if echo is None else (echo and say is not None)
    tell: Say = say if say is not None else _stderr_say
    what = what or shlex.join([str(item) for item in command[:2]])
    window = stall_seconds() if window is None else max(int(window), 1)
    attempts = max(int(attempts), 1)
    argv = [str(item) for item in command]
    if announce:
        tell("$ " + shlex.join(argv))
    stalls: List[Dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        try:
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                env=dict(environment) if environment is not None else None, cwd=cwd,
                start_new_session=os.name != "nt",
            )
        except OSError as error:
            if announce:
                tell(f"could not start: {error}")
            failed = subprocess.CompletedProcess(argv, 127, "", str(error))
            failed.start_error = error  # type: ignore[attr-defined]
            return failed
        streams: Dict[str, List[str]] = {"stdout": []}
        if not merge_stderr:
            streams["stderr"] = []
        last_output = [time.monotonic()]
        sink = current_sink()

        def pump(name: str, stream: Any) -> None:
            for line in stream:
                streams[name].append(line)
                last_output[0] = time.monotonic()
                if sink is not None:
                    sink.note(line)

        readers = [threading.Thread(target=pump, args=(name, getattr(process, name)),
                                    daemon=True) for name in streams]
        for reader in readers:
            reader.start()
        watch = ActivityWatch(process.pid)
        last_report = watch.started
        stalled: Optional[Dict[str, Any]] = None
        while True:
            try:
                process.wait(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                pass
            idle = watch.sample(last_output[0])
            now = time.monotonic()
            if now - last_report >= PROGRESS_SECONDS:
                last_report = now
                lines = sum(len(item) for item in streams.values())
                tell(f"{what} still running ({int(now - watch.started)}s, attempt {attempt}; "
                     f"{lines} output line(s), {watch.describe()}"
                     f"last progress {int(idle)}s ago)", record=False)
            if watch.watched and idle >= window:
                stalled = {"attempt": attempt, "after_s": int(now - watch.started),
                           "idle_s": int(idle), "activity": watch.activity}
                stop_group(process)
                break
        for reader in readers:
            reader.join(timeout=10)
        stdout = "".join(streams["stdout"])
        stderr = "".join(streams.get("stderr", ()))
        if stalled is None:
            if announce:
                tell(f"exit {process.returncode}")
            completed = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
            if stalls:
                completed.stall = {"stalls": stalls, "recovered": True,  # type: ignore[attr-defined]
                                   "window_s": window, "attempts": attempt}
            return completed
        stalled["output_tail"] = (stderr + stdout)[-OUTPUT_TAIL_CHARS:]
        stalls.append(stalled)
        if attempt < attempts:
            tell(f"{what} stalled: no output, CPU time or I/O for {stalled['idle_s']}s "
                 f"(window {window}s); stopped and retrying (attempt {attempt + 1}/{attempts})")
            continue
        tell(f"{what} stalled: no output, CPU time or I/O for {stalled['idle_s']}s "
             f"(window {window}s) on " + (f"all {attempts} attempts" if attempts > 1
                                         else "its only attempt"))
        completed = subprocess.CompletedProcess(argv, STALLED_EXIT_CODE, stdout, stderr)
        completed.stall = {"stalls": stalls, "recovered": False,  # type: ignore[attr-defined]
                           "window_s": window, "attempts": attempts}
        return completed
    raise AssertionError("unreachable")


def run(command: Sequence[Any], *, env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None, merge_stderr: bool = False, what: Optional[str] = None,
        attempts: int = STALL_ATTEMPTS, say: Optional[Say] = None,
        echo: Optional[bool] = None) -> subprocess.CompletedProcess:
    """:func:`run_watched` in place of ``subprocess.run(..., timeout=...)``.

    Like ``subprocess.run`` it raises ``OSError`` when the command cannot
    start; a command that stalled every time returns
    :data:`STALLED_EXIT_CODE` with its output and a ``stall`` record.
    """
    completed = run_watched([str(item) for item in command], env, say, what=what, cwd=cwd,
                            attempts=attempts, merge_stderr=merge_stderr, echo=echo)
    error = getattr(completed, "start_error", None)
    if error is not None:
        raise error
    return completed


def stall_error(completed: subprocess.CompletedProcess) -> Optional[str]:
    """``"stalled: ..."`` for a command that stalled every time, else None."""
    stall = getattr(completed, "stall", None)
    if stall is None or stall.get("recovered"):
        return None
    return (f"stalled: no output, CPU time or I/O for {stall['window_s']}s on each of "
            f"{stall['attempts']} attempt(s)")
