"""Cross-platform OS primitives (POSIX and Windows) using Python's standard library only.

Provides:
- Non-blocking advisory file locking (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows)
- Atomic file replacement with Windows `PermissionError` retry
- Private-file permission helpers
- Windows DPAPI (`CryptProtectData` / `CryptUnprotectData`) for user-scoped key storage
- Pure-Python RSASSA-PKCS1-v1_5 SHA-256 (RS256) signing for PEM keys without temp files
- Isolated process-tree execution with guaranteed teardown (POSIX session/killpg,
  Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
    import msvcrt
else:
    import fcntl
    import pwd


# MARK: - File locks & atomic writes


def try_lock(fd: int) -> None:
    """Acquire a non-blocking exclusive advisory lock on `fd`, or raise OSError."""
    if IS_WINDOWS:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(fd: int) -> None:
    """Release an advisory lock on `fd` (best effort)."""
    try:
        if IS_WINDOWS:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def atomic_replace(src: str, dst: str) -> None:
    """Replace `dst` with `src`, retrying briefly on Windows sharing violations."""
    if not IS_WINDOWS:
        os.replace(src, dst)
        return
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.015)


def chmod_private(fd: int, path: str = "") -> None:
    """Restrict file mode to owner-only on POSIX (no-op on Windows where user dirs are ACL-protected)."""
    if not IS_WINDOWS and hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)


def is_private_file(path: str) -> bool:
    """Whether `path` is a regular non-symlink file private to this user."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    if IS_WINDOWS:
        # Reject reparse points / symlinks; Windows user-profile dirs enforce per-user ACLs.
        attrs = getattr(info, "st_file_attributes", 0)
        return (attrs & 0x400) == 0  # FILE_ATTRIBUTE_REPARSE_POINT
    return info.st_uid == os.getuid() and (info.st_mode & 0o077) == 0


def real_home() -> str:
    """Current user's real home directory, ignoring HOME spoofing on POSIX."""
    if IS_WINDOWS:
        return os.path.expanduser("~")
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return os.path.expanduser("~")


# MARK: - Windows DPAPI (CurrentUser encryption)


if IS_WINDOWS:
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_DATA_BLOB),
        wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DATA_BLOB),
        wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


def dpapi_protect(data: bytes) -> bytes:
    """Encrypt `data` to the current Windows user account via DPAPI."""
    if not IS_WINDOWS:
        raise OSError("Windows DPAPI is only available on Windows")
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), buf)
    blob_out = _DATA_BLOB()
    if not _crypt32.CryptProtectData(
        ctypes.byref(blob_in), "coord-github-app", None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(blob_out.pbData, wintypes.HLOCAL))


def dpapi_unprotect(blob: bytes) -> bytes:
    """Decrypt a DPAPI blob protected for the current Windows user account."""
    if not IS_WINDOWS:
        raise OSError("Windows DPAPI is only available on Windows")
    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = _DATA_BLOB(len(blob), buf)
    blob_out = _DATA_BLOB()
    if not _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(blob_out.pbData, wintypes.HLOCAL))


# MARK: - Pure-Python RS256 signer (RFC 8017 EMSA-PKCS1-v1_5 + SHA-256)

# DER DigestInfo prefix for SHA-256 (RFC 8017 Section 9.2)
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
# RSA encryption OID: 1.2.840.113549.1.1.1
_RSA_OID = bytes.fromhex("06092a864886f70d010101")


def _der_read_tlv(data: bytes, offset: int) -> Tuple[int, bytes, int]:
    """Read one ASN.1 DER tag-length-value element starting at `offset`."""
    if offset >= len(data):
        raise ValueError("truncated DER element")
    tag = data[offset]
    offset += 1
    if offset >= len(data):
        raise ValueError("truncated DER length")
    first = data[offset]
    offset += 1
    if first < 0x80:
        length = first
    else:
        num_bytes = first & 0x7F
        if num_bytes == 0 or offset + num_bytes > len(data):
            raise ValueError("invalid DER length")
        length = int.from_bytes(data[offset:offset + num_bytes], "big")
        offset += num_bytes
    end = offset + length
    if end > len(data):
        raise ValueError("DER element exceeds input")
    return tag, data[offset:end], end


def _der_read_int(data: bytes, offset: int) -> Tuple[int, int]:
    tag, raw, end = _der_read_tlv(data, offset)
    if tag != 0x02 or not raw:
        raise ValueError("expected DER INTEGER")
    return int.from_bytes(raw, "big", signed=True), end


def _parse_rsa_pkcs1_der(der: bytes) -> Tuple[int, int, Optional[Tuple[int, int, int, int, int]]]:
    """Parse PKCS#1 RSAPrivateKey DER -> (n, d, optional CRT tuple (p, q, dp, dq, qinv))."""
    tag, seq, end = _der_read_tlv(der, 0)
    if tag != 0x30 or end != len(der):
        raise ValueError("expected DER SEQUENCE for RSAPrivateKey")
    pos = 0
    _version, pos = _der_read_int(seq, pos)
    n, pos = _der_read_int(seq, pos)
    _e, pos = _der_read_int(seq, pos)
    d, pos = _der_read_int(seq, pos)
    crt = None
    if pos < len(seq):
        try:
            p, pos = _der_read_int(seq, pos)
            q, pos = _der_read_int(seq, pos)
            dp, pos = _der_read_int(seq, pos)
            dq, pos = _der_read_int(seq, pos)
            qinv, pos = _der_read_int(seq, pos)
            if p > 1 and q > 1 and p * q == n:
                crt = (p, q, dp, dq, qinv)
        except ValueError:
            crt = None
    if n <= 0 or d <= 0:
        raise ValueError("invalid RSA private key parameters")
    return n, d, crt


def _parse_rsa_pem(pem: bytes) -> Tuple[int, int, Optional[Tuple[int, int, int, int, int]]]:
    """Parse a PKCS#1 (`BEGIN RSA PRIVATE KEY`) or PKCS#8 (`BEGIN PRIVATE KEY`) PEM."""
    lines = [line.strip() for line in pem.decode("ascii", errors="strict").splitlines() if line.strip()]
    header = next((i for i, line in enumerate(lines) if line.startswith("-----BEGIN ") and "PRIVATE KEY-----" in line), None)
    footer = next((i for i, line in enumerate(lines) if line.startswith("-----END ") and "PRIVATE KEY-----" in line), None)
    if header is None or footer is None or footer <= header + 1:
        raise ValueError("not a valid PEM private key")
    label = lines[header]
    if "ENCRYPTED" in label:
        raise ValueError("encrypted PEM private keys are not supported")
    der = base64.b64decode("".join(lines[header + 1:footer]), validate=True)
    if "RSA PRIVATE KEY" in label:
        return _parse_rsa_pkcs1_der(der)
    # PKCS#8 PrivateKeyInfo: SEQUENCE { version INTEGER, algorithm SEQUENCE { OID, ... }, privateKey OCTET STRING }
    tag, seq, end = _der_read_tlv(der, 0)
    if tag != 0x30 or end != len(der):
        raise ValueError("expected DER SEQUENCE for PKCS#8 PrivateKeyInfo")
    _version, pos = _der_read_int(seq, 0)
    alg_tag, alg_body, pos = _der_read_tlv(seq, pos)
    if alg_tag != 0x30 or _RSA_OID not in alg_body:
        raise ValueError("PKCS#8 key is not an RSA private key")
    key_tag, key_bytes, _ = _der_read_tlv(seq, pos)
    if key_tag != 0x04:
        raise ValueError("expected OCTET STRING for PKCS#8 private key")
    return _parse_rsa_pkcs1_der(key_bytes)


def rs256_sign_pem(message: bytes, pem: bytes) -> bytes:
    """Sign `message` with RSASSA-PKCS1-v1_5 + SHA-256 using an RSA PEM private key in memory."""
    n, d, crt = _parse_rsa_pem(pem)
    k = (n.bit_length() + 7) // 8
    digest_info = _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    if k < len(digest_info) + 11:
        raise ValueError("RSA key too small for SHA-256 PKCS#1 v1.5 signature")
    ps_len = k - len(digest_info) - 3
    em = b"\x00\x01" + (b"\xff" * ps_len) + b"\x00" + digest_info
    m = int.from_bytes(em, "big")
    if crt is not None:
        p, q, dp, dq, qinv = crt
        s1 = pow(m % p, dp, p)
        s2 = pow(m % q, dq, q)
        h = (qinv * (s1 - s2)) % p
        s = s2 + h * q
    else:
        s = pow(m, d, n)
    return s.to_bytes(k, "big")


# MARK: - Isolated process-tree execution


if IS_WINDOWS:
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


def _win_create_kill_job():
    """Create a Win32 Job Object that kills every assigned process & descendant on close."""
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError()
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info),
    )
    if not ok:
        error = ctypes.WinError()
        _kernel32.CloseHandle(job)
        raise error
    return job


# Only the trusted interpreter starts outside the job. It cannot spawn the requested
# command until the parent assigns it to the job and sends the argument list over stdin.
_WINDOWS_LAUNCHER = """\
import json
import subprocess
import sys

command = json.load(sys.stdin)
try:
    exit_code = subprocess.call(command, stdin=subprocess.DEVNULL)
except OSError as error:
    print(error, file=sys.stderr)
    exit_code = 127
sys.exit(exit_code)
"""


def run_isolated_process(command: List[str], cwd: str, env: Dict[str, str],
                         timeout: int) -> subprocess.CompletedProcess:
    """Run `command` in its own process group / Windows Job Object, capturing output to
    temp files so detached descendants cannot hold pipes open, and killing the entire tree
    on timeout or completion."""
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out_file, \
            tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err_file:
        if IS_WINDOWS:
            job = _win_create_kill_job()
            process = None
            timed_out = False
            try:
                process = subprocess.Popen(
                    [sys.executable, "-I", "-S", "-c", _WINDOWS_LAUNCHER],
                    cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=out_file, stderr=err_file,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                if not _kernel32.AssignProcessToJobObject(job, int(process._handle)):
                    raise OSError("could not assign the isolated launcher to its Windows Job Object")
                process.communicate(json.dumps(command).encode("ascii"), timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                _kernel32.CloseHandle(job)  # KILL_ON_JOB_CLOSE includes any live descendants.
                if process is not None:
                    # An unassigned launcher still needs killing. An assigned one
                    # may have exited as the job closed. Always confirm its exit.
                    with contextlib.suppress(OSError):
                        process.kill()
                    with contextlib.suppress(OSError):
                        process.stdin.close()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        raise OSError("the isolated Windows launcher did not terminate") from None
        else:
            process = subprocess.Popen(
                command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=out_file, stderr=err_file, text=True, start_new_session=True,
            )
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait()
        out_file.seek(0)
        err_file.seek(0)
        stdout, stderr = out_file.read(), err_file.read()
        if timed_out:
            return subprocess.CompletedProcess(command, 124, stdout, stderr or f"timed out after {timeout}s")
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
