"""macOS Keychain import via Security.framework. The credential stays in this process."""

import ctypes as C
import os
import sys
from contextlib import ExitStack
from typing import Optional


class Attribute(C.Structure):
    _fields_ = [("tag", C.c_uint32), ("length", C.c_uint32), ("data", C.c_char_p)]


class AttributeList(C.Structure):
    _fields_ = [("count", C.c_uint32), ("attrs", C.POINTER(Attribute))]


def store_generic_password(service: str, account: str, data: bytes, keychain_path: Optional[bytes] = None) -> None:
    """Create or update a generic password, allowing /usr/bin/security to retrieve it.

    A supplied file scopes both creation and updates to that keychain; otherwise use the user's default.
    """
    sec = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
    cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ref = C.c_void_p
    out_ref = C.POINTER(ref)
    cf.CFStringCreateWithCString.argtypes = [ref, C.c_char_p, C.c_uint32]
    cf.CFStringCreateWithCString.restype = ref
    cf.CFArrayCreate.argtypes = [ref, out_ref, C.c_long, ref]
    cf.CFArrayCreate.restype = ref
    cf.CFRelease.argtypes = [ref]
    cf.CFRelease.restype = None
    sec.SecKeychainCopyDefault.argtypes = [out_ref]
    sec.SecKeychainOpen.argtypes = [C.c_char_p, out_ref]
    sec.SecKeychainGetStatus.argtypes = [ref, C.POINTER(C.c_uint32)]
    sec.SecTrustedApplicationCreateFromPath.argtypes = [C.c_char_p, out_ref]
    sec.SecAccessCreate.argtypes = [ref, ref, out_ref]
    sec.SecKeychainItemCreateFromContent.argtypes = [
        C.c_uint32, C.POINTER(AttributeList), C.c_uint32, ref, ref, ref, out_ref,
    ]
    sec.SecKeychainFindGenericPassword.argtypes = [
        ref, C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p, ref, ref, out_ref,
    ]
    sec.SecKeychainItemModifyAttributesAndData.argtypes = [ref, ref, C.c_uint32, ref]

    def check(status):
        if status:
            raise OSError(f"Keychain returned OSStatus {status}")

    with ExitStack() as refs:
        def owned(pointer):
            if not pointer:
                raise OSError("Keychain allocation failed")
            refs.callback(cf.CFRelease, pointer)
            return pointer

        kc, tool, acl = ref(), ref(), ref()
        if keychain_path is None:
            check(sec.SecKeychainCopyDefault(C.byref(kc)))
        else:
            check(sec.SecKeychainOpen(keychain_path, C.byref(kc)))
        owned(kc)
        # Open also succeeds for a missing file. A subsequent create on an unusable
        # reference can silently write to the default Keychain instead.
        check(sec.SecKeychainGetStatus(kc, C.byref(C.c_uint32())))

        svc, acct = service.encode(), account.encode()
        check(sec.SecTrustedApplicationCreateFromPath(b"/usr/bin/security", C.byref(tool)))
        owned(tool)
        trusted = owned(cf.CFArrayCreate(None, C.byref(tool), 1, None))
        label = owned(cf.CFStringCreateWithCString(None, svc, 0x08000100))  # kCFStringEncodingUTF8
        check(sec.SecAccessCreate(label, trusted, C.byref(acl)))
        owned(acl)
        attrs = (Attribute * 3)(Attribute(int.from_bytes(b"svce", "big"), len(svc), svc),
                                Attribute(int.from_bytes(b"acct", "big"), len(acct), acct),
                                Attribute(int.from_bytes(b"labl", "big"), len(svc), svc))
        attr_list = AttributeList(len(attrs), attrs)
        status = sec.SecKeychainItemCreateFromContent(
            int.from_bytes(b"genp", "big"), C.byref(attr_list), len(data), data, kc, acl, None)
        if status == -25299:  # errSecDuplicateItem: update only the data, preserving the item's ACL.
            item = ref()
            check(sec.SecKeychainFindGenericPassword(kc, len(svc), svc, len(acct), acct, None, None, C.byref(item)))
            owned(item)
            status = sec.SecKeychainItemModifyAttributesAndData(item, None, len(data), data)
        check(status)


if __name__ == "__main__":
    # Run separately so a locked keychain or access prompt can be timed out.
    # Only the service, account and optional keychain path appear in argv.
    try:
        path = os.fsencode(sys.argv[3]) if len(sys.argv) > 3 else None
        store_generic_password(sys.argv[1], sys.argv[2], sys.stdin.buffer.read(), path)
    except OSError as error:
        sys.exit(str(error))
