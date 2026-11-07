"""Encrypted provider configuration: Windows DPAPI or a local Linux Fernet key."""
import base64
import ctypes
import json
import os
import threading
import tempfile
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
    if p.scheme == "http" and p.hostname not in ("127.0.0.1", "localhost", "::1", "host.docker.internal"):
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
        self.codec = "dpapi" if os.name == "nt" else "fernet"
        self.path = Path(directory) / f"providers.{self.codec}.json"
        self.key_path = Path(directory) / ".providers.key"
        self.lock = threading.RLock()

    def _fernet(self, create=False):
        from cryptography.fernet import Fernet
        if not self.key_path.exists():
            if not create:
                raise ValueError("凭据加密密钥文件缺失，请恢复数据目录中的 .providers.key")
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as output:
                    output.write(Fernet.generate_key())
        try:
            return Fernet(self.key_path.read_bytes())
        except (ValueError, OSError) as exc:
            raise ValueError("凭据加密密钥无法读取，请检查 .providers.key") from exc

    def read(self):
        with self.lock:
            if not self.path.exists():
                if self.codec == "fernet" and self.path.with_name("providers.dpapi.json").exists():
                    raise ValueError("检测到 Windows 凭据，请在原 Windows 账户解密并导入容器")
                return json.loads(json.dumps(DEFAULTS))
            wrapper = json.loads(self.path.read_text(encoding="utf-8"))
            if self.codec == "dpapi":
                raw = dpapi(base64.b64decode(wrapper["encrypted"]), True)
            else:
                from cryptography.fernet import InvalidToken
                try:
                    raw = self._fernet().decrypt(wrapper["encrypted"].encode())
                except InvalidToken:
                    raise ValueError("凭据解密失败，请核对配置文件和 .providers.key 是否配套") from None
            return json.loads(raw)

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
            raw = json.dumps(data).encode()
            encrypted = (base64.b64encode(dpapi(raw)).decode() if self.codec == "dpapi"
                         else self._fernet(create=True).encrypt(raw).decode())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="providers-", suffix=".tmp", dir=self.path.parent)
            temp = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump({"version": 1, "codec": self.codec, "encrypted": encrypted}, output)
                os.replace(temp, self.path)
            finally:
                temp.unlink(missing_ok=True)
            return self.public(data)
