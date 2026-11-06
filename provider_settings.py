"""Current-user encrypted provider configuration. No plaintext secrets on disk."""
import base64
import ctypes
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit


DEFAULTS = {
    "nai": {"url": "", "key": ""},
    "assistant": {"url": "", "key": "", "model": ""},
}


def validate_url(value):
    value = str(value).strip().rstrip("/")
    p = urlsplit(value)
    if (p.scheme not in ("https", "http") or not p.hostname or p.username or p.password
            or p.query or p.fragment):
        raise ValueError("接口地址需为完整的 HTTP(S) 地址，不包含密钥、查询参数或片段")
    if p.scheme == "http" and p.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("远程接口请使用 HTTPS；本机接口支持 HTTP")
    return value


def dpapi(data, decrypt=False):
    if os.name != "nt":
        raise ValueError("API 凭据保存需要 Windows 当前用户加密")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    func = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    func.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                     ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    func.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not func(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ValueError("Windows 凭据解密失败，请在当前账户重新填写 API Key" if decrypt
                         else "Windows 凭据加密失败")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel.LocalFree(result.pbData)


class ProviderSettings:
    def __init__(self, directory):
        self.path = Path(directory) / "providers.dpapi.json"
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.exists():
                return json.loads(json.dumps(DEFAULTS))
            wrapper = json.loads(self.path.read_text(encoding="utf-8"))
            return json.loads(dpapi(base64.b64decode(wrapper["encrypted"]), True))

    @staticmethod
    def public(data):
        return {name: {**{k: v for k, v in value.items() if k != "key"},
                       "has_key": bool(value.get("key")),
                       "key_mask": "••••••••" if value.get("key") else ""}
                for name, value in data.items()}

    def save(self, payload):
        with self.lock:
            data = self.read()
            for name in DEFAULTS:
                patch = payload.get(name)
                if patch is None:
                    continue
                if not isinstance(patch, dict):
                    raise ValueError("无效的接口配置")
                value = data[name]
                if "url" in patch:
                    value["url"] = validate_url(patch["url"]) if patch["url"] else ""
                if patch.get("clear_key"):
                    value["key"] = ""
                if patch.get("key"):
                    value["key"] = str(patch["key"]).strip()
                if name == "assistant" and "model" in patch:
                    value["model"] = str(patch["model"]).strip()[:250]
            encrypted = base64.b64encode(dpapi(json.dumps(data).encode())).decode()
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps({"version": 1, "encrypted": encrypted}), encoding="utf-8")
            os.replace(temp, self.path)
            return self.public(data)
