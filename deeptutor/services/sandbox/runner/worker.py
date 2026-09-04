"""Fail-closed Linux launcher for one sandbox-runner job.

The HTTP broker starts this module as a fresh process and passes only pinned
directory descriptors plus a JSON configuration.  The launcher is deliberately
separate from the threaded broker: credential and Landlock setup never runs in
``subprocess.preexec_fn``.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import platform
import resource
import signal
import sys
from typing import Any

SECURITY_PROFILE = "landlock-v6-fd-v3"
MIN_LANDLOCK_ABI = 6

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446

_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_CREATE_RULESET_ERRATA = 2
REQUIRED_LANDLOCK_ERRATA = 1 << (3 - 1)
_LANDLOCK_RULE_PATH_BENEATH = 1

_LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
_LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
_LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
_LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
_LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
_LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
_LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
_LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
_LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
_LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
_LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
_LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
_LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
_LANDLOCK_ACCESS_FS_REFER = 1 << 13
_LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14
_LANDLOCK_ACCESS_FS_IOCTL_DEV = 1 << 15

_LANDLOCK_ACCESS_NET_BIND_TCP = 1 << 0
_LANDLOCK_ACCESS_NET_CONNECT_TCP = 1 << 1
_LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
_LANDLOCK_SCOPE_SIGNAL = 1 << 1

_FS_HANDLED = (1 << 16) - 1
_FS_READ_ONLY = (
    _LANDLOCK_ACCESS_FS_EXECUTE | _LANDLOCK_ACCESS_FS_READ_FILE | _LANDLOCK_ACCESS_FS_READ_DIR
)
_FS_READ_WRITE = (
    _FS_READ_ONLY
    | _LANDLOCK_ACCESS_FS_WRITE_FILE
    | _LANDLOCK_ACCESS_FS_REMOVE_DIR
    | _LANDLOCK_ACCESS_FS_REMOVE_FILE
    | _LANDLOCK_ACCESS_FS_MAKE_DIR
    | _LANDLOCK_ACCESS_FS_MAKE_REG
    | _LANDLOCK_ACCESS_FS_MAKE_SYM
    | _LANDLOCK_ACCESS_FS_REFER
    | _LANDLOCK_ACCESS_FS_TRUNCATE
)

_PR_SET_PDEATHSIG = 1
_PR_SET_DUMPABLE = 4
_PR_CAPBSET_READ = 23
_PR_CAPBSET_DROP = 24
_PR_SET_NO_NEW_PRIVS = 38
_PR_GET_NO_NEW_PRIVS = 39
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_IS_SET = 1
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2

_BPF_LD_W_ABS = 0x20
_BPF_JMP_JEQ_K = 0x15
_BPF_JMP_JGE_K = 0x35
_BPF_RET_K = 0x06
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_DATA_ARCH_OFFSET = 4
SECCOMP_POLICY = "deny-sockets-ipc-path-metadata-v3"

_LINUX_CAPABILITY_VERSION_3 = 0x20080522
_MAX_CAPABILITY = 63

_libc = ctypes.CDLL(None, use_errno=True)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
    ]


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int32)]


class _CapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def _raise_errno(operation: str) -> None:
    error = ctypes.get_errno()
    raise OSError(error, f"{operation}: {os.strerror(error)}")


def landlock_abi() -> int:
    """Return the kernel Landlock ABI, or raise when Landlock is unavailable."""
    result = _libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        0,
        0,
        _LANDLOCK_CREATE_RULESET_VERSION,
    )
    if result < 0:
        _raise_errno("Landlock ABI query")
    return int(result)


def landlock_errata() -> int:
    """Return the kernel's fixed Landlock errata bitmask."""
    result = _libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        0,
        0,
        _LANDLOCK_CREATE_RULESET_ERRATA,
    )
    if result < 0:
        _raise_errno("Landlock errata query")
    return int(result)


def no_new_privs() -> int:
    result = _libc.prctl(_PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0)
    if result < 0:
        _raise_errno("PR_GET_NO_NEW_PRIVS")
    return int(result)


def capability_state() -> dict[str, list[int]]:
    """Return every process capability set as capability numbers."""
    header = _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapData * 2)()
    if _libc.capget(ctypes.byref(header), ctypes.byref(data)) != 0:
        _raise_errno("capget")

    def _numbers(field: str) -> list[int]:
        value = int(getattr(data[0], field)) | (int(getattr(data[1], field)) << 32)
        return [cap for cap in range(_MAX_CAPABILITY + 1) if value & (1 << cap)]

    bounding: list[int] = []
    ambient: list[int] = []
    for cap in range(_MAX_CAPABILITY + 1):
        ctypes.set_errno(0)
        bounded = _libc.prctl(_PR_CAPBSET_READ, cap, 0, 0, 0)
        if bounded == 1:
            bounding.append(cap)
        elif bounded < 0 and ctypes.get_errno() not in (0, errno.EINVAL):
            _raise_errno("PR_CAPBSET_READ")

        ctypes.set_errno(0)
        raised = _libc.prctl(
            _PR_CAP_AMBIENT,
            _PR_CAP_AMBIENT_IS_SET,
            cap,
            0,
            0,
        )
        if raised == 1:
            ambient.append(cap)
        elif raised < 0 and ctypes.get_errno() not in (0, errno.EINVAL):
            _raise_errno("PR_CAP_AMBIENT_IS_SET")

    return {
        "effective": _numbers("effective"),
        "permitted": _numbers("permitted"),
        "inheritable": _numbers("inheritable"),
        "bounding": bounding,
        "ambient": ambient,
    }


def _seccomp_syscalls() -> tuple[int, set[int]]:
    """Return ``(audit_arch, denied_nrs)`` for supported images."""
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return (
            0xC000003E,
            {
                29,  # shmget
                30,  # shmat
                31,  # shmctl
                41,  # socket
                53,  # socketpair
                64,  # semget
                65,  # semop
                66,  # semctl
                67,  # shmdt
                68,  # msgget
                69,  # msgsnd
                70,  # msgrcv
                71,  # msgctl
                89,  # readlink
                90,  # chmod
                91,  # fchmod
                92,  # chown
                93,  # fchown
                94,  # lchown
                101,  # ptrace
                109,  # setpgid
                112,  # setsid
                132,  # utime
                141,  # setpriority
                142,  # sched_setparam
                144,  # sched_setscheduler
                188,  # setxattr
                189,  # lsetxattr
                190,  # fsetxattr
                191,  # getxattr
                192,  # lgetxattr
                193,  # fgetxattr
                194,  # listxattr
                195,  # llistxattr
                196,  # flistxattr
                197,  # removexattr
                198,  # lremovexattr
                199,  # fremovexattr
                203,  # sched_setaffinity
                220,  # semtimedop
                235,  # utimes
                240,  # mq_open
                241,  # mq_unlink
                242,  # mq_timedsend
                243,  # mq_timedreceive
                244,  # mq_notify
                245,  # mq_getsetattr
                248,  # add_key
                249,  # request_key
                250,  # keyctl
                251,  # ioprio_set
                253,  # inotify_init
                254,  # inotify_add_watch
                255,  # inotify_rm_watch
                256,  # migrate_pages
                260,  # fchownat
                261,  # futimesat
                267,  # readlinkat
                268,  # fchmodat
                272,  # unshare
                274,  # get_robust_list
                279,  # move_pages
                280,  # utimensat
                294,  # inotify_init1
                298,  # perf_event_open
                300,  # fanotify_init
                301,  # fanotify_mark
                302,  # prlimit64
                303,  # name_to_handle_at
                304,  # open_by_handle_at
                308,  # setns
                310,  # process_vm_readv
                311,  # process_vm_writev
                312,  # kcmp
                314,  # sched_setattr
                424,  # pidfd_send_signal
                425,  # io_uring_setup
                426,  # io_uring_enter
                427,  # io_uring_register
                434,  # pidfd_open
                438,  # pidfd_getfd
                440,  # process_madvise
                448,  # process_mrelease
                452,  # fchmodat2
            },
        )
    if machine in {"aarch64", "arm64"}:
        return (
            0xC00000B7,
            {
                5,  # setxattr
                6,  # lsetxattr
                7,  # fsetxattr
                8,  # getxattr
                9,  # lgetxattr
                10,  # fgetxattr
                11,  # listxattr
                12,  # llistxattr
                13,  # flistxattr
                14,  # removexattr
                15,  # lremovexattr
                16,  # fremovexattr
                26,  # inotify_init1
                27,  # inotify_add_watch
                28,  # inotify_rm_watch
                30,  # ioprio_set
                52,  # fchmod
                53,  # fchmodat
                54,  # fchownat
                55,  # fchown
                78,  # readlinkat
                88,  # utimensat
                97,  # unshare
                100,  # get_robust_list
                117,  # ptrace
                118,  # sched_setparam
                119,  # sched_setscheduler
                122,  # sched_setaffinity
                140,  # setpriority
                154,  # setpgid
                157,  # setsid
                180,  # mq_open
                181,  # mq_unlink
                182,  # mq_timedsend
                183,  # mq_timedreceive
                184,  # mq_notify
                185,  # mq_getsetattr
                186,  # msgget
                187,  # msgctl
                188,  # msgrcv
                189,  # msgsnd
                190,  # semget
                191,  # semctl
                192,  # semtimedop
                193,  # semop
                194,  # shmget
                195,  # shmctl
                196,  # shmat
                197,  # shmdt
                198,  # socket
                199,  # socketpair
                217,  # add_key
                218,  # request_key
                219,  # keyctl
                238,  # migrate_pages
                239,  # move_pages
                241,  # perf_event_open
                261,  # prlimit64
                262,  # fanotify_init
                263,  # fanotify_mark
                264,  # name_to_handle_at
                265,  # open_by_handle_at
                268,  # setns
                270,  # process_vm_readv
                271,  # process_vm_writev
                272,  # kcmp
                274,  # sched_setattr
                424,  # pidfd_send_signal
                425,  # io_uring_setup
                426,  # io_uring_enter
                427,  # io_uring_register
                434,  # pidfd_open
                438,  # pidfd_getfd
                440,  # process_madvise
                448,  # process_mrelease
                452,  # fchmodat2
            },
        )
    raise RuntimeError(f"unsupported seccomp architecture: {machine}")


def _install_job_seccomp() -> str:
    """Deny sockets, cross-process controls, IPC, and metadata bypasses.

    Landlock deliberately does not mediate chmod/chown, timestamps, extended
    attributes, or filesystem-notification watches.  Every account is stored
    with the same host UID in the shared volume, so those syscalls must be
    denied for the complete job domain; a known sibling path must not become an
    integrity or activity side channel.
    """
    audit_arch, denied_syscalls = _seccomp_syscalls()
    denied_action = _SECCOMP_RET_ERRNO | errno.EPERM

    instructions: list[_SockFilter] = [
        _SockFilter(_BPF_LD_W_ABS, 0, 0, _SECCOMP_DATA_ARCH_OFFSET),
        _SockFilter(_BPF_JMP_JEQ_K, 1, 0, audit_arch),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_KILL_PROCESS),
        _SockFilter(_BPF_LD_W_ABS, 0, 0, 0),
    ]
    if audit_arch == 0xC000003E:
        # Reject the x32 ABI bit so alternate syscall numbers cannot bypass the
        # x86_64 deny list on kernels built with CONFIG_X86_X32.
        instructions.extend(
            [
                _SockFilter(_BPF_JMP_JGE_K, 0, 1, 0x40000000),
                _SockFilter(_BPF_RET_K, 0, 0, denied_action),
            ]
        )
    for syscall_nr in sorted(denied_syscalls):
        instructions.append(_SockFilter(_BPF_JMP_JEQ_K, 0, 1, syscall_nr))
        instructions.append(_SockFilter(_BPF_RET_K, 0, 0, denied_action))
    instructions.append(_SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW))

    filters = (_SockFilter * len(instructions))(*instructions)
    program = _SockFprog(len(instructions), filters)
    if _libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(program), 0, 0) != 0:
        _raise_errno("PR_SET_SECCOMP")
    return SECCOMP_POLICY


def _set_rlimits(memory_mb: int, cpu_seconds: int, nofile: int) -> None:
    memory_bytes = memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _drop_credentials(uid: int, gid: int) -> None:
    if _libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        _raise_errno("PR_CAP_AMBIENT_CLEAR_ALL")
    os.setgroups([])
    for cap in range(_MAX_CAPABILITY + 1):
        ctypes.set_errno(0)
        if _libc.prctl(_PR_CAPBSET_DROP, cap, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            if error != errno.EINVAL:
                raise OSError(error, f"PR_CAPBSET_DROP({cap}): {os.strerror(error)}")

    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    if _libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        _raise_errno("PR_SET_DUMPABLE")
    if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        _raise_errno("PR_SET_NO_NEW_PRIVS")

    if os.getresuid() != (uid, uid, uid):
        raise RuntimeError(f"UID drop did not stick: {os.getresuid()!r}")
    if os.getresgid() != (gid, gid, gid):
        raise RuntimeError(f"GID drop did not stick: {os.getresgid()!r}")
    if os.getgroups():
        raise RuntimeError(f"supplementary groups remain: {os.getgroups()!r}")
    if any(capability_state().values()):
        raise RuntimeError("one or more capability sets remain after credential drop")
    if no_new_privs() != 1:
        raise RuntimeError("no_new_privs is not set")


def _add_path_rule(ruleset_fd: int, path_fd: int, allowed_access: int) -> None:
    attr = _PathBeneathAttr(allowed_access, path_fd, 0)
    if (
        _libc.syscall(
            _SYS_LANDLOCK_ADD_RULE,
            ruleset_fd,
            _LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(attr),
            0,
        )
        != 0
    ):
        _raise_errno("landlock_add_rule")


def _open_rule(path: str, allowed_access: int) -> tuple[int, int] | None:
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    return fd, allowed_access


def _restrict_filesystem(
    workdir_fd: int, scratch_fd: int, read_only_fds: list[int]
) -> tuple[int, int]:
    abi = landlock_abi()
    if abi < MIN_LANDLOCK_ABI:
        raise RuntimeError(f"Landlock ABI {abi} is below required ABI {MIN_LANDLOCK_ABI}")
    errata = landlock_errata()
    if errata & REQUIRED_LANDLOCK_ERRATA != REQUIRED_LANDLOCK_ERRATA:
        raise RuntimeError("Landlock erratum 3 is not fixed by this kernel")

    attr = _RulesetAttr(
        _FS_HANDLED,
        _LANDLOCK_ACCESS_NET_BIND_TCP | _LANDLOCK_ACCESS_NET_CONNECT_TCP,
        _LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET | _LANDLOCK_SCOPE_SIGNAL,
    )
    ruleset_fd = _libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr),
        ctypes.sizeof(attr),
        0,
    )
    if ruleset_fd < 0:
        _raise_errno("landlock_create_ruleset")

    opened: list[int] = []
    try:
        rules: list[tuple[int, int]] = [
            (workdir_fd, _FS_READ_WRITE),
            (scratch_fd, _FS_READ_WRITE),
            *((fd, _FS_READ_ONLY) for fd in read_only_fds),
        ]
        for path, access in (
            ("/usr", _FS_READ_ONLY),
            ("/etc", _FS_READ_ONLY),
            ("/proc/self", _FS_READ_ONLY),
            ("/var/cache/fontconfig", _FS_READ_ONLY),
            ("/dev/null", _LANDLOCK_ACCESS_FS_READ_FILE | _LANDLOCK_ACCESS_FS_WRITE_FILE),
            ("/dev/zero", _LANDLOCK_ACCESS_FS_READ_FILE),
            ("/dev/random", _LANDLOCK_ACCESS_FS_READ_FILE),
            ("/dev/urandom", _LANDLOCK_ACCESS_FS_READ_FILE),
        ):
            opened_rule = _open_rule(path, access)
            if opened_rule is not None:
                opened.append(opened_rule[0])
                rules.append(opened_rule)

        for path_fd, access in rules:
            _add_path_rule(ruleset_fd, path_fd, access)

        if _libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) != 0:
            _raise_errno("landlock_restrict_self")
    finally:
        os.close(ruleset_fd)
        for fd in opened:
            os.close(fd)
    return abi, errata


def _write_status(status_fd: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n"
    while data:
        written = os.write(status_fd, data)
        data = data[written:]


def _launch(config: dict[str, Any], status_fd: int) -> None:
    workdir_fd = int(config["workdir_fd"])
    scratch_fd = int(config["scratch_fd"])
    read_only_fds = [int(fd) for fd in config.get("read_only_fds", [])]
    uid = int(config["uid"])
    gid = int(config["gid"])

    parent_pid = os.getppid()
    if parent_pid != int(config["broker_pid"]):
        raise RuntimeError("worker parent does not match the broker")
    os.setsid()

    _set_rlimits(
        int(config["memory_mb"]),
        int(config["cpu_seconds"]),
        int(config["nofile"]),
    )
    os.umask(0o077)
    os.fchdir(workdir_fd)
    _drop_credentials(uid, gid)
    # Linux clears PDEATHSIG when real/effective credentials change. Install it
    # only after the irreversible UID/GID drop, then close the race by checking
    # that the trusted broker is still our parent.
    if _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        _raise_errno("PR_SET_PDEATHSIG")
    if os.getppid() != parent_pid:
        raise RuntimeError("broker exited while the worker was starting")
    abi, errata = _restrict_filesystem(workdir_fd, scratch_fd, read_only_fds)
    seccomp_policy = _install_job_seccomp()

    null_fd = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
    os.dup2(null_fd, 0)
    os.close(null_fd)

    attestation = {
        "ok": True,
        "security_profile": SECURITY_PROFILE,
        "landlock_abi": abi,
        "landlock_errata": errata,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "groups": os.getgroups(),
        "no_new_privs": no_new_privs(),
        "capabilities": capability_state(),
        "seccomp_policy": seccomp_policy,
    }
    _write_status(status_fd, attestation)
    os.set_inheritable(status_fd, False)

    for fd in {workdir_fd, scratch_fd, *read_only_fds}:
        if fd > 2 and fd != status_fd:
            os.close(fd)

    env = {str(key): str(value) for key, value in config["env"].items()}
    scratch_path = str(config["scratch_path"])
    env.update(
        {"HOME": scratch_path, "TMPDIR": scratch_path, "TMP": scratch_path, "TEMP": scratch_path}
    )
    env.pop("PWD", None)

    argv = [str(item) for item in config.get("argv") or []]
    if not argv:
        argv = ["/bin/sh", "-c", str(config["command"])]
    try:
        os.execvpe(argv[0], argv, env)
    except Exception as exc:
        _write_status(
            status_fd,
            {"ok": False, "error": f"exec failed: {type(exc).__name__}: {exc}"},
        )
        raise


def main() -> None:
    status_fd_text = os.environ.pop("DEEPTUTOR_RUNNER_STATUS_FD", "")
    if not status_fd_text:
        raise SystemExit("worker status fd is missing")
    status_fd = int(status_fd_text)
    try:
        config = json.load(sys.stdin)
        if not isinstance(config, dict):
            raise ValueError("worker config must be a JSON object")
        _launch(config, status_fd)
    except BaseException as exc:
        try:
            _write_status(
                status_fd,
                {"ok": False, "error": f"worker setup failed: {type(exc).__name__}: {exc}"},
            )
        except OSError:
            pass
        os._exit(125)


if __name__ == "__main__":
    main()
