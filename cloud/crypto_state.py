"""Encrypted public state. No plaintext location files are written."""
import base64
import json
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

ITERATIONS = 600000
AAD = b"locator-state-v1"
def _b64(value):
    return base64.b64encode(value).decode("ascii")
def _key(password, salt, iterations):
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=iterations).derive(password.encode("utf-8"))
def encrypt(state, password):
    if len(password) < 12:
        raise ValueError("访问密码至少12个字符")
    salt, nonce = os.urandom(16), os.urandom(12)
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
    data = AESGCM(_key(password, salt, ITERATIONS)).encrypt(nonce, raw, AAD)
    return {"version": 1, "iterations": ITERATIONS, "salt": _b64(salt),
            "nonce": _b64(nonce), "ciphertext": _b64(data)}
def decrypt(envelope, password):
    if envelope.get("version") != 1 or envelope.get("iterations") != ITERATIONS:
        raise ValueError("不支持的密文格式")
    b = lambda k: base64.b64decode(envelope[k], validate=True)
    raw = AESGCM(_key(password, b("salt"), ITERATIONS)).decrypt(b("nonce"), b("ciphertext"), AAD)
    return json.loads(raw)
