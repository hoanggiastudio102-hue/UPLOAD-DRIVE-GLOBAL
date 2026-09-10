"""Shared transport and OS-protected storage for DriveDrop.

Google credentials never belong in client configuration.  SecretStore protects
secrets for the current OS user; it cannot protect against code running as that
user or an administrator.  The broker's state directory must not be distributed.
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


MAX_JSON_BYTES = 1024 * 1024


class ApiError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.message = message
        self.status = status


def canonical_json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _reject_link(path: Path) -> None:
    """Reject a symlink or Windows reparse point at this path, when it exists."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ApiError("A state file or directory must not be a link/reparse point.")


def _windows_restrict(path: Path, is_directory: bool = False) -> None:
    """Set a protected DACL granting only the current user and SYSTEM access."""
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                          ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.SetFileSecurityW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    sid_text = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    try:
        length = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not length.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER starts with SID_AND_ATTRIBUTES; its first field is PSID.
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_string = ctypes.wstring_at(sid_text)
        flags = "OICI" if is_directory else ""
        sddl = f"D:P(A;{flags};FA;;;{sid_string})(A;{flags};FA;;;SY)"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        if sid_text.value:
            kernel.LocalFree(sid_text)
        kernel.CloseHandle(token)


def _restrict(path: Path, is_directory: bool = False) -> None:
    _reject_link(path)
    if sys.platform == "win32":
        _windows_restrict(path, is_directory)
    else:
        os.chmod(path, 0o700 if is_directory else 0o600)


def _private_directory(directory: Path) -> Path:
    directory = Path(directory).absolute()
    _reject_link(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _restrict(directory, is_directory=True)
    return directory


def secure_directory(directory: Path) -> Path:
    """Create/protect an application-owned state directory, not its ancestors.

    Call only for a dedicated DriveDrop directory, never a watch folder or a
    general user directory. New SQLite files then inherit Windows protection;
    on macOS the 0700 directory prevents access by other users.
    """
    return _private_directory(Path(directory))


def _atomic_bytes(path: Path, data: bytes) -> None:
    path = Path(path).absolute()
    _reject_link(path)
    _reject_link(path.parent)
    if not path.parent.exists():
        _private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        # Restrict before placing any sensitive bytes into the temporary file.
        _restrict(temporary)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if sys.platform != "win32":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_json(path: Path, obj) -> None:
    _atomic_bytes(Path(path), canonical_json(obj) + b"\n")


def load_json(path: Path, default=None):
    path = Path(path)
    _reject_link(path)
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data, len(data) or 1)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi(data: bytes, entropy: bytes, decrypt: bool) -> bytes:
    from ctypes import wintypes
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p,
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    source, source_buffer = _blob(data)
    salt, salt_buffer = _blob(entropy)
    output = _DataBlob()
    try:
        if decrypt:
            success = crypt.CryptUnprotectData(ctypes.byref(source), None,
                ctypes.byref(salt), None, None, 1, ctypes.byref(output))
        else:
            success = crypt.CryptProtectData(ctypes.byref(source), "DriveDrop secret",
                ctypes.byref(salt), None, None, 1, ctypes.byref(output))
        if not success:
            error = ctypes.get_last_error()
            raise ApiError(f"Windows DPAPI refused the secret operation (error {error}). "
                           "Run DriveDrop in the intended Windows user's normal session.")
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if output.pbData:
            # Clear decrypted data before returning the native allocation.
            ctypes.memset(output.pbData, 0, output.cbData)
            kernel.LocalFree(output.pbData)


class _MacKeychain:
    """Use the OS Keychain C API directly; no passwords in shell arguments."""
    NOT_FOUND = -25300
    DUPLICATE = -25299

    def __init__(self, service: str):
        self.service = service.encode("utf-8")
        self.security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.core.CFRelease.argtypes = [ctypes.c_void_p]
        self.core.CFRelease.restype = None
        self.security.SecKeychainFindGenericPassword.argtypes = [ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p)]
        self.security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainAddGenericPassword.argtypes = [ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        self.security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self.security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        self.security.SecKeychainItemDelete.restype = ctypes.c_int32
        self.security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.security.SecKeychainItemFreeContent.restype = ctypes.c_int32

    @staticmethod
    def _check(status: int) -> None:
        if status:
            raise ApiError(f"macOS Keychain refused the operation (OSStatus {status}). "
                           "Unlock the login Keychain and allow this application.")

    def _find(self, name: str, read: bool):
        account = name.encode("utf-8")
        length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(None,
            len(self.service), self.service, len(account), account,
            ctypes.byref(length) if read else None,
            ctypes.byref(data) if read else None, ctypes.byref(item))
        if status == self.NOT_FOUND:
            return None, None
        self._check(status)
        try:
            result = ctypes.string_at(data, length.value).decode("utf-8") if read else None
        except BaseException:
            if item.value:
                self.core.CFRelease(item)
            raise
        finally:
            if data.value:
                self.security.SecKeychainItemFreeContent(None, data)
        return result, item

    def get(self, name: str) -> str | None:
        result, item = self._find(name, True)
        if item:
            self.core.CFRelease(item)
        return result

    def set(self, name: str, value: str) -> None:
        account, password = name.encode("utf-8"), value.encode("utf-8")
        _, item = self._find(name, False)
        if item:
            try:
                self._check(self.security.SecKeychainItemModifyAttributesAndData(
                    item, None, len(password), password))
            finally:
                self.core.CFRelease(item)
            return
        created = ctypes.c_void_p()
        status = self.security.SecKeychainAddGenericPassword(None,
            len(self.service), self.service, len(account), account,
            len(password), password, ctypes.byref(created))
        if created.value:
            self.core.CFRelease(created)
        if status == self.DUPLICATE:
            # A second process can create this item between the lookup and add.
            _, item = self._find(name, False)
            if item:
                try:
                    self._check(self.security.SecKeychainItemModifyAttributesAndData(
                        item, None, len(password), password))
                finally:
                    self.core.CFRelease(item)
                return
        self._check(status)

    def delete(self, name: str) -> None:
        _, item = self._find(name, False)
        if item:
            try:
                status = self.security.SecKeychainItemDelete(item)
                if status != self.NOT_FOUND:
                    self._check(status)
            finally:
                self.core.CFRelease(item)


class SecretStore:
    """Per-user DPAPI on Windows; login Keychain on macOS; no plaintext fallback."""
    def __init__(self, directory: Path, namespace: str):
        if sys.platform not in ("win32", "darwin"):
            raise ApiError("Protected secret storage requires Windows or macOS.")
        if not isinstance(namespace, str) or not namespace or len(namespace) > 200:
            raise ValueError("A nonempty secret-store namespace is required.")
        self.directory = _private_directory(Path(directory))
        self.namespace = namespace
        self._lock = threading.RLock()
        self._keychain = None
        if sys.platform == "darwin":
            identity = hashlib.sha256(str(self.directory).encode("utf-8")).hexdigest()[:24]
            self._keychain = _MacKeychain(f"com.drivedrop.{namespace}.{identity}")

    def _identity(self, name: str) -> tuple[Path, bytes]:
        if not isinstance(name, str) or not name or len(name) > 1000:
            raise ValueError("A nonempty secret name is required.")
        entropy = hashlib.sha256(canonical_json([self.namespace, name])).digest()
        return self.directory / (entropy.hex() + ".dpapi"), entropy

    def get(self, name: str) -> str | None:
        with self._lock:
            path, entropy = self._identity(name)
            if self._keychain is not None:
                return self._keychain.get(name)
            _reject_link(path)
            try:
                encrypted = path.read_bytes()
            except FileNotFoundError:
                return None
            return _dpapi(encrypted, entropy, decrypt=True).decode("utf-8")

    def set(self, name: str, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("SecretStore values must be strings.")
        with self._lock:
            path, entropy = self._identity(name)
            if self._keychain is not None:
                self._keychain.set(name, value)
            else:
                _atomic_bytes(path, _dpapi(value.encode("utf-8"), entropy, decrypt=False))

    def delete(self, name: str) -> None:
        with self._lock:
            path, _ = self._identity(name)
            if self._keychain is not None:
                self._keychain.delete(name)
            else:
                _reject_link(path)
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass


def sign_request(secret: str, method: str, path: str, timestamp: str,
                 nonce: str, body: bytes) -> str:
    message = "\n".join((method, path, str(timestamp), nonce,
                         hashlib.sha256(body).hexdigest())).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


class _PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, pin: str):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Trust the enrolled leaf pin instead of a public CA.
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(host, port=port, timeout=30, context=context)
        self.pin = pin

    def connect(self):
        super().connect()
        certificate = self.sock.getpeercert(binary_form=True)
        received = hashlib.sha256(certificate).hexdigest() if certificate else ""
        if not hmac.compare_digest(received, self.pin):
            self.close()
            raise ApiError("Broker certificate does not match the enrolled SHA-256 pin. "
                           "No HTTP data was sent.")


def pinned_request(base_url: str, pin: str, method: str, path: str,
                   payload=None, device_id: str | None = None,
                   secret: str | None = None, tls_mode: str = "pinned") -> dict:
    if tls_mode not in ("pinned", "public_ca"):
        raise ApiError("Unsupported broker TLS trust mode.")
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", pin):
        raise ApiError("The broker certificate SHA-256 pin is invalid.")
    try:
        target = urlsplit(base_url)
        port = target.port or 443
    except (ValueError, TypeError) as exc:
        raise ApiError("The broker HTTPS address is invalid.") from exc
    if (target.scheme != "https" or not target.hostname or target.username is not None
            or target.password is not None or target.query or target.fragment
            or target.path not in ("", "/")):
        raise ApiError("The broker address must be an HTTPS origin, without credentials or a path.")
    if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
            or any(ord(char) < 32 or ord(char) == 127 for char in path) or "#" in path):
        raise ApiError("Invalid API request path.")
    if not isinstance(method, str) or not re.fullmatch(r"[A-Z]+", method):
        raise ApiError("HTTP method must use uppercase letters.")
    if (device_id is None) != (secret is None):
        raise ApiError("Both device ID and secret are required for an authenticated request.")
    body = b"" if payload is None else canonical_json(payload)
    if len(body) > MAX_JSON_BYTES:
        raise ApiError("The API JSON request exceeds 1 MiB.")
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "Content-Length": str(len(body)), "Connection": "close"}
    if device_id is not None:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", device_id):
            raise ApiError("Invalid device ID.")
        timestamp, nonce = str(int(time.time())), uuid.uuid4().hex
        headers.update({"X-Device-Id": device_id, "X-Timestamp": timestamp,
                        "X-Nonce": nonce,
                        "X-Signature": sign_request(secret, method, path, timestamp, nonce, body)})
    if tls_mode == "public_ca":
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connection = http.client.HTTPSConnection(target.hostname, port, timeout=30, context=context)
    else:
        connection = _PinnedConnection(target.hostname, port, pin.lower())
    try:
        # Verification is inside connect(), so even an automatic reconnect cannot
        # transmit headers or the enrollment code before pinning its own socket.
        connection.connect()
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ApiError("HTTP redirects are not allowed for the broker.", response.status)
        raw = response.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ApiError("The broker JSON response exceeds 1 MiB.", response.status)
        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ApiError("The broker returned an invalid JSON response.", response.status) from exc
        if not isinstance(result, dict):
            raise ApiError("The broker returned an invalid JSON object.", response.status)
        if not 200 <= response.status < 300:
            message = result.get("error", result.get("message", "Broker request failed."))
            if not isinstance(message, str):
                message = "Broker request failed."
            raise ApiError(message[:512], response.status)
        return result
    except ApiError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise ApiError("Could not reach the broker over verified HTTPS.") from exc
    finally:
        connection.close()


def make_certificate(directory: Path) -> tuple[Path, Path, str]:
    """Create/persist a private self-signed broker TLS identity and its leaf pin."""
    directory = _private_directory(Path(directory))
    certificate_path, key_path = directory / "broker-cert.pem", directory / "broker-key.pem"
    _reject_link(certificate_path)
    _reject_link(key_path)
    if certificate_path.exists() != key_path.exists():
        raise ApiError("Broker TLS identity is incomplete. Restore its certificate/key pair.")
    if certificate_path.exists():
        _restrict(key_path)
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        encoding, form = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        if key.public_key().public_bytes(encoding, form) != certificate.public_key().public_bytes(encoding, form):
            raise ApiError("Broker certificate and private key do not match.")
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DriveDrop private broker")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.IPAddress(ipaddress.ip_address("::1"))]), critical=False)
            .sign(key, hashes.SHA256()))
        _atomic_bytes(key_path, key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        _atomic_bytes(certificate_path, certificate.public_bytes(serialization.Encoding.PEM))
    pin = certificate.fingerprint(hashes.SHA256()).hex()
    return certificate_path, key_path, pin
