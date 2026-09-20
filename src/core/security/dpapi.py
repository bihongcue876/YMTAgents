"""Windows DPAPI 封装（ctypes + 标准库；spec v0.0.2 §2）。

- `CryptProtectData` / `CryptUnprotectData`（crypt32.dll），**CurrentUser 作用域**
  （不带 `CRYPTPROTECT_LOCAL_MACHINE`）→ 密文与当前 Windows 账户绑定，换账户/换机器不可解。
- 免口令：不引入主密码、不引入第三方加密库（标准库无 AEAD；`cryptography` 无 win 轮子）。
- 失败一律抛错，**绝不回退明文**。
"""

from __future__ import annotations

import ctypes
import sys

from core.security.errors import SecretError, SecretUnavailable

#: 用途熵（单一来源）：不同用途的密文不可互换。
ENTROPY_VAULT = b"ymt.vault.1"

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _make_blob(data: bytes) -> tuple[_DataBlob, object]:
    if not data:
        return _DataBlob(0, None), None
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


class DpapiBox:
    """DPAPI 封存/解封；`available()` 探测本机能力。"""

    def __init__(self) -> None:
        self._lib: object | None = None

    def _load(self):
        if self._lib is not None:
            return self._lib
        if sys.platform != "win32":
            raise SecretUnavailable("本机加密仅 Windows 可用")
        try:
            crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
            crypt32.CryptProtectData.restype = ctypes.c_int
            crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(_DataBlob),
                ctypes.c_wchar_p,
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(_DataBlob),
            ]
            crypt32.CryptUnprotectData.restype = ctypes.c_int
            crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(_DataBlob),
            ]
        except (AttributeError, OSError) as exc:
            raise SecretUnavailable(f"本机加密不可用：{type(exc).__name__}") from exc
        self._lib = crypt32
        return crypt32

    def available(self) -> bool:
        try:
            self._load()
            return True
        except SecretUnavailable:
            return False

    @staticmethod
    def _free(blob: _DataBlob) -> None:
        if blob.pbData:
            ctypes.windll.kernel32.LocalFree(  # type: ignore[attr-defined]
                ctypes.cast(blob.pbData, ctypes.c_void_p)
            )

    def seal(self, data: bytes, *, entropy: bytes = b"") -> bytes:
        lib = self._load()
        blob_in, _keep_in = _make_blob(data)
        blob_ent, _keep_ent = _make_blob(entropy)
        blob_out = _DataBlob()
        ok = lib.CryptProtectData(
            ctypes.byref(blob_in),
            None,
            ctypes.byref(blob_ent) if entropy else None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise SecretUnavailable("加密失败（DPAPI 不可用）")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self._free(blob_out)

    def unseal(self, blob: bytes, *, entropy: bytes = b"") -> bytes:
        lib = self._load()
        blob_in, _keep_in = _make_blob(blob)
        blob_ent, _keep_ent = _make_blob(entropy)
        blob_out = _DataBlob()
        ok = lib.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            ctypes.byref(blob_ent) if entropy else None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise SecretError("secret_decrypt_failed", "解密失败（密文不属于当前账户或已损坏）")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self._free(blob_out)