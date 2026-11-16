#!/usr/bin/env python3
"""Out-of-process headless owner and watchdog; workload starts only after registration.

The supervisor is disposable, but the child is gated until both its identity is
on disk and the independent watchdog is watching the supervisor's pipe. EOF
on that pipe, watchdog death, or the deadline stops the process group.
"""
from __future__ import annotations

import json
import ctypes
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import headless_run as hr
import run_lease


def identity(pid):
    if type(pid) is not int or pid <= 0:
        return None
    value = run_lease.pid_start(pid)
    if value is not None:
        return 'proc:' + value
    if sys.platform != 'darwin':
        return None
    # libproc reports microsecond process birth time; ps lstart has only
    # second resolution and can confuse a rapidly reused PID with our child.
    class ProcBSDInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint32) for name in (
            'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid',
            'svuid', 'svgid', 'rfu_1')] + [
                ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)] + [
                (name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'pjobc', 'e_tdev', 'e_tpgid', 'nice')
            ] + [('start_sec', ctypes.c_uint64), ('start_usec', ctypes.c_uint64)]
    try:
        lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        info = ProcBSDInfo()
        size = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info) or info.pid != pid or not info.start_sec:
            return None
        return f'darwin:{info.start_sec}:{info.start_usec}'
    except (OSError, AttributeError):
        return None


def same_process(pid, expected):
    return expected is not None and identity(pid) == expected


def stop(pid, expected):
    if not same_process(pid, expected):
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    end = time.monotonic() + 2
    while time.monotonic() < end and same_process(pid, expected):
        time.sleep(.05)
    if same_process(pid, expected):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        end = time.monotonic() + 2
        while time.monotonic() < end and same_process(pid, expected):
            time.sleep(.05)
    fields = run_lease._proc_stat(pid)
    return not same_process(pid, expected) or bool(fields and fields[0] == 'Z')


def gate(fd, argv):
    try:
        if os.read(fd, 1) != b'G':
            return 125
    finally:
        os.close(fd)
    os.execvpe(argv[0], argv, os.environ)


def guard(fd, ready, pid, expected, deadline, path):
    os.write(ready, b'R')
    os.close(ready)
    reason = None
    while True:
        delay = 0.2 if deadline is None else max(0, min(.2, deadline - time.monotonic()))
        readable, _, _ = select.select([fd], [], [], delay)
        if readable:
            data = os.read(fd, 1)
            if data == b'D':
                return 0
            if not data:
                reason = 'SUPERVISOR_LOST'
                break
        if deadline is not None and time.monotonic() >= deadline:
            reason = hr.TIME_LIMIT_CODE
            break
    stopped = stop(pid, expected)
    with hr.locked_record(path):
        record = hr.load_record(path)
        if record and record.get('pid') == pid and record.get('process_identity') == expected:
            if reason == hr.TIME_LIMIT_CODE and stopped:
                # Process has already been stopped; retain the existing terminalization path.
                hr._stop_locked(record, 0, reason)
            elif stopped:
                record.update(status='interrupted', pid=None, stop_reason=reason)
            else:
                record.update(status='launch_uncertain', stop_reason=reason)
            hr.save_record(path, record)
    return 0


def supervise(config):
    path = Path(config['path'])
    gate_read, gate_write = os.pipe()
    watch_read, watch_write = os.pipe()
    ready_read, ready_write = os.pipe()
    child = watcher = None
    try:
        with open(config['log_path'], 'ab') as log:
            child = subprocess.Popen([sys.executable, __file__, 'gate', str(gate_read), *config['argv']],
                                     cwd=config['cwd'], stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(gate_read,))
        os.close(gate_read)
        gate_read = -1
        token = identity(child.pid)
        supervisor_token = identity(os.getpid())
        if token is None or supervisor_token is None:
            raise RuntimeError('cannot verify process identity')
        with hr.locked_record(path):
            record = hr.load_record(path)
            if not record or record.get('status') != 'launching':
                raise RuntimeError('launch intent changed before registration')
            if config.get('continuing'):
                run_lease.acquire(record['run_id'], record['lease_owner'], pid=child.pid, handoff=True)
            record.update(pid=child.pid, pid_start=run_lease.pid_start(child.pid), process_identity=token,
                          supervisor_pid=os.getpid(), supervisor_identity=supervisor_token,
                          status='launching', launches=record['launches'] + 1, started_at=hr._now(), exit_code=None)
            hr.save_record(path, record)
        watcher = subprocess.Popen([sys.executable, __file__, 'guard', str(watch_read), str(ready_write),
                                    str(child.pid), token, str(config['monotonic_deadline']) if config.get('monotonic_deadline') is not None else '', str(path)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   pass_fds=(watch_read, ready_write), start_new_session=True)
        os.close(watch_read); watch_read = -1
        os.close(ready_write); ready_write = -1
        readable, _, _ = select.select([ready_read], [], [], 10)
        if not readable or os.read(ready_read, 1) != b'R':
            raise RuntimeError('watchdog registration failed')
        watcher_identity = identity(watcher.pid)
        if watcher_identity is None or watcher.poll() is not None:
            raise RuntimeError('cannot verify watchdog identity')
        with hr.locked_record(path):
            record = hr.load_record(path)
            if not record or record.get('status') != 'launching' or record.get('pid') != child.pid:
                raise RuntimeError('launch cancelled before release')
            record.update(status='launched', watchdog_pid=watcher.pid, watchdog_identity=watcher_identity,
                          outcome='resumed' if config['continuing'] else 'launched')
            hr.save_record(path, record)
        os.write(gate_write, b'G')
        os.close(gate_write); gate_write = -1
        while child.poll() is None:
            if watcher.poll() is not None:
                stopped = stop(child.pid, token)
                child.wait()
                with hr.locked_record(path):
                    record = hr.load_record(path)
                    if record and record.get('status') in {'launched', 'launch_uncertain'}:
                        if stopped:
                            record.update(status='interrupted', pid=None, stop_reason='WATCHDOG_LOST')
                        else:
                            record.update(status='launch_uncertain', stop_reason='WATCHDOG_LOST')
                        hr.save_record(path, record)
                return 1
            time.sleep(.05)
        try:
            os.write(watch_write, b'D')
        except BrokenPipeError:
            pass
        watcher.wait()
        with hr.locked_record(path):
            record = hr.load_record(path)
            if record:
                record['exit_code'] = child.returncode
                if record.get('status') in {'launched', 'launch_uncertain'}:
                    record.update(status='exited', pid=None)
                    hr.refresh(record)
                hr.save_record(path, record)
        return child.returncode or 0
    except BaseException as exc:
        if child is not None:
            if gate_write >= 0:
                os.close(gate_write)
                gate_write = -1
            stop(child.pid, token if 'token' in locals() else None)
            child.wait()
        with hr.locked_record(path):
            record = hr.load_record(path)
            if record and record.get('status') == 'launching':
                record.update(status='interrupted', pid=None, stop_reason='SUPERVISOR_START_FAILED')
                if isinstance(exc, run_lease.LeaseError):
                    record.update(lease_handoff_error=exc.code, outcome='lease_handoff_failed')
                hr.save_record(path, record)
        raise
    finally:
        for fd in (gate_read, gate_write, watch_read, watch_write, ready_read, ready_write):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


if __name__ == '__main__':
    if sys.argv[1] == 'gate':
        sys.exit(gate(int(sys.argv[2]), sys.argv[3:]))
    if sys.argv[1] == 'guard':
        sys.exit(guard(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5],
                       float(sys.argv[6]) if sys.argv[6] else None, Path(sys.argv[7])))
    sys.exit(supervise(json.load(sys.stdin)))
