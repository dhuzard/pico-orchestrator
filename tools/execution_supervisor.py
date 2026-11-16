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
        # Start ticks alone can repeat after reboot or in another PID namespace.
        host = run_lease.host_id()
        return f'proc:{host}:{value}' if host else None
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


def session_members(sid):
    """Return live (pid, process group, state) entries, or None if observation failed."""
    if type(sid) is not int or sid <= 0 or sid == os.getsid(0):
        return None
    try:
        # macOS ps may show an unusable zero session ID; getsid(2) provides
        # the POSIX session identity on both Darwin and Linux.
        result = subprocess.run(['ps', '-axo', 'pid=,stat='], capture_output=True, text=True, timeout=5)
        if result.returncode:
            return None
        found = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                return None
            pid = int(parts[0])
            if parts[1].startswith('Z'):
                continue
            try:
                if os.getsid(pid) == sid:
                    found.append((pid, os.getpgid(pid), parts[1]))
            except ProcessLookupError:
                continue
        return found
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def stop(pid, expected, grace=0.3):
    """Stop the entire anchored session; never signal after losing anchor identity.

    Freeze every member before SIGKILL, repeating discovery until all are
    stopped. Linux uses PID handles to avoid reuse when signaling. Darwin
    supports only the anchor group and fails closed for secondary groups.
    """
    def anchor_dead_or_gone():
        current = identity(pid)
        if current is not None and current != expected:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        if current is None or current != expected:
            return False
        fields = run_lease._proc_stat(pid)
        if fields:
            return fields[0] == 'Z'
        if sys.platform == 'darwin':
            try:
                result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                                        capture_output=True, text=True, timeout=5)
                return result.returncode == 0 and result.stdout.strip().startswith('Z')
            except (OSError, subprocess.TimeoutExpired):
                return False
        return False

    members = session_members(pid)
    if members == []:
        return anchor_dead_or_gone()
    if members is None or not same_process(pid, expected):
        return False

    def empty_after_competing_stop():
        end = time.monotonic() + 1
        while time.monotonic() < end:
            current = session_members(pid)
            if current == [] and anchor_dead_or_gone():
                return True
            if current is None:
                return False
            time.sleep(.02)
        return False

    linux_pidfd = (sys.platform.startswith('linux') and hasattr(os, 'pidfd_open')
                   and hasattr(signal, 'pidfd_send_signal'))
    if not linux_pidfd and sys.platform != 'darwin':
        return False

    def signal_members(entries, sig):
        if not same_process(pid, expected):
            return False
        if not linux_pidfd:
            # Darwin has no atomic process handles. Only the anchor's own
            # group can be signalled safely while the anchor pins its ID.
            if any(entry[1] != pid for entry in entries):
                return False
            if pid == os.getpgrp():
                return False
            try:
                os.killpg(pid, sig)
                return True
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
        for member, _, _ in entries:
            if not same_process(pid, expected):
                return False
            before = identity(member)
            if before is None:
                try:
                    os.kill(member, 0)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    return False
                return False
            try:
                handle = os.pidfd_open(member, 0)
            except ProcessLookupError:
                continue
            except OSError:
                return False
            try:
                if identity(member) != before:
                    return False
                try:
                    if os.getsid(member) != pid:
                        continue
                except ProcessLookupError:
                    continue
                signal.pidfd_send_signal(handle, sig)
            except ProcessLookupError:
                continue
            except OSError:
                return False
            finally:
                os.close(handle)
        return True

    if not signal_members(members, signal.SIGTERM):
        return empty_after_competing_stop()
    end = time.monotonic() + max(0, grace)
    while time.monotonic() < end:
        members = session_members(pid)
        if members is None:
            return False
        if not any(entry[0] != pid for entry in members):
            break
        time.sleep(.05)
    # Freeze before enumeration; SIGSTOP cannot be ignored. A process that
    # cannot be observed stopped leaves the execution uncertain.
    end = time.monotonic() + 3
    while True:
        members = session_members(pid)
        if members == []:
            return anchor_dead_or_gone()
        if members is None:
            return False
        if not same_process(pid, expected):
            return empty_after_competing_stop()
        if not signal_members(members, signal.SIGSTOP):
            return empty_after_competing_stop()
        members = session_members(pid)
        if members is None:
            return False
        if all(entry[2][0] in ('T', 't') for entry in members):
            break
        if time.monotonic() >= end:
            return False
        time.sleep(.02)
    if linux_pidfd:
        if not signal_members([entry for entry in members if entry[0] != pid], signal.SIGKILL):
            return empty_after_competing_stop()
        if not signal_members([entry for entry in members if entry[0] == pid], signal.SIGKILL):
            return empty_after_competing_stop()
    elif not signal_members(members, signal.SIGKILL):
        return empty_after_competing_stop()
    end = time.monotonic() + 3
    while time.monotonic() < end:
        members = session_members(pid)
        if members == [] and anchor_dead_or_gone():
            return True
        if members is None:
            return False
        time.sleep(.05)
    return False


def gate(fd, result_fd, argv):
    try:
        if os.read(fd, 1) != b'G':
            return 125
    finally:
        os.close(fd)
    # A caught signal handler is reset to its default by exec, unlike SIG_IGN.
    # The anchor survives TERM while the workload receives it normally.
    signal.signal(signal.SIGTERM, lambda *_: None)
    try:
        workload = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
        result = workload.wait()
    except OSError:
        result = 127
    try:
        os.write(result_fd, (str(result) + '\n').encode())
    except BrokenPipeError:
        pass
    finally:
        os.close(result_fd)
    # Keep the session identity pinned until the supervisor cleans all members.
    while True:
        signal.pause()


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
    result_read, result_write = os.pipe()
    child = watcher = None
    try:
        with open(config['log_path'], 'ab') as log:
            child = subprocess.Popen([sys.executable, __file__, 'gate', str(gate_read), str(result_write), *config['argv']],
                                     cwd=config['cwd'], stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(gate_read, result_write))
        os.close(gate_read)
        gate_read = -1
        os.close(result_write)
        result_write = -1
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
        workload_exit = None
        while True:
            if watcher.poll() is not None:
                stopped = stop(child.pid, token)
                if stopped:
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
            readable, _, _ = select.select([result_read], [], [], .05)
            if readable:
                result = os.read(result_read, 32)
                if result:
                    try:
                        workload_exit = int(result.strip())
                    except ValueError:
                        pass
                break
            if child.poll() is not None:
                break
        stopped = stop(child.pid, token)
        if stopped:
            child.wait()
            try:
                os.write(watch_write, b'D')
            except BrokenPipeError:
                pass
        else:
            os.close(watch_write)
            watch_write = -1
        watcher.wait()
        with hr.locked_record(path):
            record = hr.load_record(path)
            if record:
                record['exit_code'] = workload_exit if workload_exit is not None else child.returncode
                if record.get('status') in {'launched', 'launch_uncertain'}:
                    if stopped:
                        record.update(status='exited', pid=None)
                        hr.refresh(record)
                        if workload_exit is None and (record.get('run') or {}).get('status') in {None, 'IN_PROGRESS'}:
                            record['status'] = 'interrupted'
                    else:
                        record.update(status='launch_uncertain', stop_reason='TERMINATION_UNCONFIRMED')
                hr.save_record(path, record)
        return workload_exit or 0
    except BaseException as exc:
        stopped = child is None
        if child is not None:
            if gate_write >= 0:
                os.close(gate_write)
                gate_write = -1
            stopped = stop(child.pid, token if 'token' in locals() else None)
            if child.poll() is not None:
                child.wait()
        with hr.locked_record(path):
            record = hr.load_record(path)
            if record and record.get('status') == 'launching':
                if stopped:
                    record.update(status='interrupted', pid=None, stop_reason='SUPERVISOR_START_FAILED')
                else:
                    record.update(status='launch_uncertain', stop_reason='SUPERVISOR_START_FAILED')
                if isinstance(exc, run_lease.LeaseError):
                    record.update(lease_handoff_error=exc.code, outcome='lease_handoff_failed')
                hr.save_record(path, record)
        raise
    finally:
        for fd in (gate_read, gate_write, watch_read, watch_write, ready_read, ready_write, result_read, result_write):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


if __name__ == '__main__':
    if sys.argv[1] == 'gate':
        sys.exit(gate(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4:]))
    if sys.argv[1] == 'guard':
        sys.exit(guard(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5],
                       float(sys.argv[6]) if sys.argv[6] else None, Path(sys.argv[7])))
    sys.exit(supervise(json.load(sys.stdin)))
