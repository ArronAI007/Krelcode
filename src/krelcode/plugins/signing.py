"""Canonical plugin signatures and a dependency-free RFC 8032 verifier.

Only verification is needed by the client. ``sign``/``public_key_from_seed`` are
small publishing helpers used by release tooling and interoperability tests; the
agent never loads or stores private keys.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .integrity import PluginIntegrity, PluginIntegrityError, package_integrity


SIGNATURE_SCHEMA = 1
SIGNATURE_ALGORITHM = "ed25519"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PluginSignatureError(ValueError):
    """A signature or signed metadata is malformed or invalid."""


# Ed25519 field and group constants, RFC 8032 section 5.1.
_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)
_IDENTITY = (0, 1, 1, 0)


def _inv(value: int) -> int:
    return pow(value % _P, _P - 2, _P)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    x = (u * pow(v, 3, _P) * pow(u * pow(v, 7, _P) % _P, (_P - 5) // 8, _P)) % _P
    if (v * x * x - u) % _P != 0:
        x = x * _I % _P
    if (v * x * x - u) % _P != 0:
        return None
    if x == 0 and sign:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _decode_point(encoded: bytes) -> tuple[int, int, int, int] | None:
    if len(encoded) != 32:
        return None
    value = int.from_bytes(encoded, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    if x is None:
        return None
    return x, y, 1, x * y % _P


def _encode_point(point: tuple[int, int, int, int]) -> bytes:
    x, y, z, _ = point
    z_inv = _inv(z)
    x = x * z_inv % _P
    y = y * z_inv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _point_add(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    x1, y1, z1, t1 = left
    x2, y2, z2, t2 = right
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = 2 * _D * t1 * t2 % _P
    d = 2 * z1 * z2 % _P
    e = b - a
    f = d - c
    g = d + c
    h = b + a
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _point_double(point: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x, y, z, _ = point
    a = x * x % _P
    b = y * y % _P
    c = 2 * z * z % _P
    h = a + b
    e = h - (x + y) * (x + y)
    g = a - b
    f = c + g
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _scalar_mult(scalar: int, point: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    result = _IDENTITY
    current = point
    value = int(scalar)
    while value:
        if value & 1:
            result = _point_add(result, current)
        current = _point_double(current)
        value >>= 1
    return result


def _points_equal(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> bool:
    x1, y1, z1, _ = left
    x2, y2, z2, _ = right
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def _base_point() -> tuple[int, int, int, int]:
    y = 4 * _inv(5) % _P
    x = _recover_x(y, 0)
    if x is None:
        raise AssertionError("invalid Ed25519 base point")
    return x, y, 1, x * y % _P


_BASE = _base_point()


def _decode_scalar(value: bytes) -> int:
    return int.from_bytes(value, "little")


def _encode_scalar(value: int) -> bytes:
    return (value % _Q).to_bytes(32, "little")


def _expand_seed(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be exactly 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar_bytes = bytearray(digest[:32])
    scalar_bytes[0] &= 248
    scalar_bytes[31] &= 63
    scalar_bytes[31] |= 64
    return int.from_bytes(scalar_bytes, "little"), digest[32:]


def public_key_from_seed(seed: bytes) -> bytes:
    scalar, _ = _expand_seed(seed)
    return _encode_point(_scalar_mult(scalar, _BASE))


def sign(message: bytes, seed: bytes) -> bytes:
    scalar, prefix = _expand_seed(seed)
    public = _encode_point(_scalar_mult(scalar, _BASE))
    nonce = int.from_bytes(
        hashlib.sha512(prefix + message).digest(), "little") % _Q
    encoded_r = _encode_point(_scalar_mult(nonce, _BASE))
    challenge = int.from_bytes(
        hashlib.sha512(encoded_r + public + message).digest(), "little") % _Q
    encoded_s = _encode_scalar(nonce + challenge * scalar)
    return encoded_r + encoded_s


def verify(message: bytes, signature: bytes, public_key: bytes) -> bool:
    """Strict RFC 8032 verification, including subgroup checks."""

    try:
        if len(signature) != 64 or len(public_key) != 32:
            return False
        encoded_r = signature[:32]
        s = _decode_scalar(signature[32:])
        if s >= _Q:
            return False
        point_a = _decode_point(public_key)
        point_r = _decode_point(encoded_r)
        if point_a is None or point_r is None:
            return False
        # Reject small-order and non-prime-subgroup public keys/signatures.
        if _points_equal(point_a, _IDENTITY) or _points_equal(point_r, _IDENTITY):
            return False
        if not _points_equal(_scalar_mult(_Q, point_a), _IDENTITY):
            return False
        if not _points_equal(_scalar_mult(_Q, point_r), _IDENTITY):
            return False
        challenge = int.from_bytes(
            hashlib.sha512(encoded_r + public_key + message).digest(), "little") % _Q
        left = _scalar_mult(s, _BASE)
        right = _point_add(point_r, _scalar_mult(challenge, point_a))
        return _points_equal(left, right)
    except (TypeError, ValueError, OverflowError):
        return False


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _b64decode(value: str, field: str, expected_length: int | None = None) -> bytes:
    try:
        raw = base64.b64decode(str(value), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PluginSignatureError(f"{field} 不是合法 Base64") from exc
    if expected_length is not None and len(raw) != expected_length:
        raise PluginSignatureError(
            f"{field} 长度应为 {expected_length} bytes，实际 {len(raw)}")
    return raw


def encode_public_key(public_key: bytes) -> str:
    if len(public_key) != 32:
        raise ValueError("Ed25519 public key must be exactly 32 bytes")
    return base64.b64encode(public_key).decode("ascii")


def decode_public_key(value: str) -> bytes:
    return _b64decode(value, "public_key", 32)


@dataclass(frozen=True)
class SignatureMetadata:
    schema: int
    plugin: str
    version: str
    algorithm: str
    key_id: str
    registry: str
    manifest_sha256: str
    files_sha256: str
    files_count: int
    signature: bytes

    def payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "plugin": self.plugin,
            "version": self.version,
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "registry": self.registry,
            "manifest_sha256": self.manifest_sha256,
            "files_sha256": self.files_sha256,
            "files_count": self.files_count,
        }

    def signed_bytes(self) -> bytes:
        return canonical_json_bytes(self.payload())

    def to_dict(self) -> dict[str, Any]:
        value = self.payload()
        value["signature"] = base64.b64encode(self.signature).decode("ascii")
        return value


def _require_text(raw: Mapping[str, Any], key: str, *, max_length: int = 256) -> str:
    value = str(raw.get(key) or "").strip()
    if not value:
        raise PluginSignatureError(f"签名字段 {key} 不能为空")
    if len(value) > max_length:
        raise PluginSignatureError(f"签名字段 {key} 过长")
    return value


def parse_signature_metadata(raw: Mapping[str, Any]) -> SignatureMetadata:
    if not isinstance(raw, Mapping):
        raise PluginSignatureError("plugin.sig.json 必须是 JSON object")
    try:
        schema = int(raw.get("schema", 0))
    except (TypeError, ValueError) as exc:
        raise PluginSignatureError("签名 schema 必须是整数") from exc
    if schema != SIGNATURE_SCHEMA:
        raise PluginSignatureError(f"不支持签名 schema={schema}")
    algorithm = _require_text(raw, "algorithm")
    if algorithm != SIGNATURE_ALGORITHM:
        raise PluginSignatureError(f"不支持签名算法: {algorithm}")
    try:
        files_count = int(raw.get("files_count", -1))
    except (TypeError, ValueError) as exc:
        raise PluginSignatureError("files_count 必须是整数") from exc
    if files_count < 0:
        raise PluginSignatureError("files_count 不能为负数")
    manifest_sha256 = _require_text(raw, "manifest_sha256", max_length=64)
    files_sha256 = _require_text(raw, "files_sha256", max_length=64)
    if not _SHA256_RE.fullmatch(manifest_sha256):
        raise PluginSignatureError("manifest_sha256 必须是 64 位小写 hex")
    if not _SHA256_RE.fullmatch(files_sha256):
        raise PluginSignatureError("files_sha256 必须是 64 位小写 hex")
    return SignatureMetadata(
        schema=schema,
        plugin=_require_text(raw, "plugin", max_length=64),
        version=_require_text(raw, "version", max_length=128),
        algorithm=algorithm,
        key_id=_require_text(raw, "key_id", max_length=128),
        registry=_require_text(raw, "registry", max_length=128),
        manifest_sha256=manifest_sha256,
        files_sha256=files_sha256,
        files_count=files_count,
        signature=_b64decode(str(raw.get("signature") or ""), "signature", 64),
    )


def read_signature(root: Path) -> SignatureMetadata | None:
    path = Path(root) / "plugin.sig.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise PluginSignatureError("plugin.sig.json 必须是普通文件")
    try:
        if path.stat().st_size > 64 * 1024:
            raise PluginSignatureError("plugin.sig.json 过大")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PluginSignatureError(f"无法读取 plugin.sig.json: {exc}") from exc
    return parse_signature_metadata(raw)


def create_signature_document(
    *,
    plugin: str,
    version: str,
    key_id: str,
    registry: str,
    integrity: PluginIntegrity,
    seed: bytes,
) -> dict[str, Any]:
    """Create a detached signature document for release tooling/tests."""

    public_key = public_key_from_seed(seed)
    metadata = SignatureMetadata(
        schema=SIGNATURE_SCHEMA,
        plugin=plugin,
        version=version,
        algorithm=SIGNATURE_ALGORITHM,
        key_id=key_id,
        registry=registry,
        manifest_sha256=integrity.manifest_sha256,
        files_sha256=integrity.files_sha256,
        files_count=integrity.files_count,
        signature=b"\x00" * 64,
    )
    signature = sign(metadata.signed_bytes(), seed)
    # A release helper may validate its own output before returning it.
    if not verify(metadata.signed_bytes(), signature, public_key):
        raise PluginSignatureError("内部 Ed25519 自检失败")
    return SignatureMetadata(
        **{**metadata.__dict__, "signature": signature}
    ).to_dict()


@dataclass(frozen=True)
class PluginVerification:
    status: str
    key_id: str = ""
    registry: str = ""
    signer: str = ""
    integrity_digest: str = ""
    manifest_sha256: str = ""
    files_sha256: str = ""
    files_count: int = 0
    message: str = ""

    @property
    def valid(self) -> bool:
        return self.status == "valid"


def verify_plugin(
    root: Path,
    *,
    expected_name: str,
    expected_version: str,
    public_key: bytes | None,
    signer: str = "",
    key_revoked: bool = False,
) -> PluginVerification:
    root = Path(root)
    try:
        metadata = read_signature(root)
    except PluginSignatureError as exc:
        return PluginVerification("invalid", message=str(exc))
    if metadata is None:
        return PluginVerification("unsigned")
    try:
        integrity = package_integrity(root)
    except PluginIntegrityError as exc:
        return PluginVerification(
            "invalid", key_id=metadata.key_id, registry=metadata.registry,
            message=str(exc))
    base = dict(
        key_id=metadata.key_id,
        registry=metadata.registry,
        signer=signer,
        integrity_digest=integrity.digest,
        manifest_sha256=integrity.manifest_sha256,
        files_sha256=integrity.files_sha256,
        files_count=integrity.files_count,
    )
    if metadata.plugin != expected_name:
        return PluginVerification(
            "invalid", message="签名 plugin 与 manifest name 不一致", **base)
    if metadata.version != expected_version:
        return PluginVerification(
            "invalid", message="签名 version 与 manifest version 不一致", **base)
    if metadata.manifest_sha256 != integrity.manifest_sha256:
        return PluginVerification(
            "invalid", message="manifest Hash 不匹配", **base)
    if metadata.files_sha256 != integrity.files_sha256:
        return PluginVerification(
            "invalid", message="组件文件 Hash 不匹配", **base)
    if metadata.files_count != integrity.files_count:
        return PluginVerification(
            "invalid", message="组件文件数量不匹配", **base)
    if key_revoked:
        return PluginVerification(
            "revoked", message="签名公钥已撤销", **base)
    if public_key is None:
        return PluginVerification(
            "unknown_key", message="签名公钥不在信任库中", **base)
    if not verify(metadata.signed_bytes(), metadata.signature, public_key):
        return PluginVerification(
            "invalid", message="Ed25519 签名验证失败", **base)
    return PluginVerification(
        "valid", message="签名有效", **base)
