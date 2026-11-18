"""Encryption for the current Windows user, compatible with existing TRACE data."""


def protect_for_current_user(data: bytes) -> bytes:
    """Encrypt a payload with Windows DPAPI and suppress interactive prompts."""
    import win32crypt

    return win32crypt.CryptProtectData(data, None, None, None, None, 1)


def unprotect_for_current_user(data: bytes) -> bytes:
    """Decrypt a payload in the Windows profile that recorded it."""
    import win32crypt

    return win32crypt.CryptUnprotectData(data, None, None, None, 1)[1]
