"""Exercise the macOS import helper and its reader using only private temporary Keychains."""

import base64
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coordlib import macos_keychain


SERVICE = "coord-test-service"
ACCOUNT = "coord-test-account"


@unittest.skipUnless(sys.platform == "darwin", "requires the macOS Security framework")
class MacOSKeychainTests(unittest.TestCase):
    def setUp(self):
        # Security.framework treats login and System Keychain paths specially. Use a private
        # filename under /tmp, independent of HOME and the caller's temporary directory.
        directory = tempfile.TemporaryDirectory(dir="/tmp", prefix="coord-keychain-")
        self.addCleanup(directory.cleanup)
        self.keychain = Path(directory.name) / "scratch.keychain"

    def create_keychain(self):
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        security.SecKeychainCreate.argtypes = [
            ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_bool,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        foundation.CFRelease.argtypes = [ctypes.c_void_p]
        foundation.CFRelease.restype = None
        keychain_ref = ctypes.c_void_p()
        password = b"temporary-test-password"
        status = security.SecKeychainCreate(
            os.fsencode(self.keychain), len(password), password, False, None, ctypes.byref(keychain_ref),
        )
        self.assertEqual(status, 0)
        foundation.CFRelease(keychain_ref)

    def read(self, account=ACCOUNT):
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", SERVICE, "-a", account, "-w", self.keychain],
            stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=20,
        )
        return result.stdout

    def test_create_and_replace_are_readable_by_security(self):
        self.create_keychain()
        other_account = "other-test-account"
        neighbor = subprocess.run(
            [sys.executable, "-I", "-S", macos_keychain.__file__, SERVICE, other_account, self.keychain],
            input=b"independent-item", capture_output=True, timeout=20,
        )
        self.assertEqual(neighbor.returncode, 0, neighbor.stderr)
        # The import helper receives the base64 of a PEM; large payloads must survive both writes.
        for size, fill in [(1650, b"a"), (6000, b"b")]:
            with self.subTest(size=size):
                value = base64.b64encode(fill * size)
                macos_keychain.store_generic_password(SERVICE, ACCOUNT, value, os.fsencode(self.keychain))
                self.assertEqual(self.read(), value + b"\n")
        self.assertEqual(self.read(other_account), b"independent-item\n")

    def test_missing_or_invalid_keychain_fails_before_native_item_creation(self):
        # The native creator can fall back to the default Keychain. Disable that
        # single write while exercising the real framework's open and status checks.
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        security.SecKeychainItemCreateFromContent = Mock(side_effect=AssertionError("unexpected write"))
        for state in ("missing", "invalid"):
            with self.subTest(state=state):
                if state == "invalid":
                    self.keychain.write_bytes(b"invalid test database")
                with patch.object(ctypes, "CDLL", side_effect=[security, foundation]):
                    with self.assertRaisesRegex(OSError, "OSStatus"):
                        macos_keychain.store_generic_password(SERVICE, ACCOUNT, b"fixture", os.fsencode(self.keychain))
                security.SecKeychainItemCreateFromContent.assert_not_called()
