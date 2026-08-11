"""使用当前 Windows 用户 DPAPI 保护 API Key，并提供安全展示尾号。"""

import base64
import binascii
import ctypes
from ctypes import wintypes

DPAPI_PREFIX = "dpapi:"
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DataBlob(ctypes.Structure):
    """Windows DPAPI 使用的二进制缓冲结构。"""

    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DataBlob),
    wintypes.LPCWSTR,
    ctypes.POINTER(DataBlob),
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(DataBlob),
]
crypt32.CryptProtectData.restype = wintypes.BOOL
crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DataBlob),
    ctypes.POINTER(wintypes.LPWSTR),
    ctypes.POINTER(DataBlob),
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(DataBlob),
]
crypt32.CryptUnprotectData.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
kernel32.LocalFree.restype = wintypes.HLOCAL


def _input_blob(data: bytes) -> tuple[DataBlob, ctypes.Array[ctypes.c_char]]:
    """构造在 DPAPI 调用期间保持有效的输入缓冲。"""

    buffer = ctypes.create_string_buffer(data)
    blob = DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    return blob, buffer


def _raise_last_windows_error() -> None:
    """把 DPAPI 的线程本地错误码转换为 Python 异常。"""

    raise ctypes.WinError(ctypes.get_last_error())


def protect_api_key(api_key: str) -> str:
    """使用当前 Windows 用户凭据加密 API Key。"""

    if not api_key:
        raise ValueError("API Key 不能为空")

    input_blob, _buffer = _input_blob(api_key.encode("utf-8"))
    output_blob = DataBlob()
    succeeded = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "LoreWeave API Key",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    if not succeeded:
        _raise_last_windows_error()

    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return DPAPI_PREFIX + base64.urlsafe_b64encode(encrypted).decode("ascii")
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


def unprotect_api_key(secret_ref: str) -> str:
    """解密由当前 Windows 用户 DPAPI 生成的密钥引用。"""

    if not secret_ref.startswith(DPAPI_PREFIX):
        raise ValueError("不支持的密钥引用格式")

    try:
        encrypted = base64.b64decode(
            secret_ref.removeprefix(DPAPI_PREFIX),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError("密钥引用内容无效") from exc

    input_blob, _buffer = _input_blob(encrypted)
    output_blob = DataBlob()
    succeeded = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    if not succeeded:
        _raise_last_windows_error()

    try:
        plaintext = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return plaintext.decode("utf-8")
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


def api_key_tail(api_key: str) -> str:
    """仅返回前端允许展示的密钥最后四位。"""

    return api_key[-4:] if api_key else ""
