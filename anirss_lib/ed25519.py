"""Ed25519 signature checks (RFC 8032), in plain Python so anirss keeps its
zero runtime dependencies. Only verification: anirss checks signed endpoint
repos and never signs anything.

The check is the strict one ed25519-dalek's `verify_strict` makes, which is
what AniBeam uses on the same files: S below the group order, neither the key
nor R of small order, and the recomputed R equal to the signature's R byte for
byte, so a non-canonical encoding never passes. A key or R whose y is not
below p is refused outright."""

import hashlib

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

# Points are extended homogeneous coordinates (X, Y, Z, T): x = X/Z, y = Y/Z,
# x*y = T/Z.
_ZERO = (0, 1, 1, 0)


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _add(p: tuple, q: tuple) -> tuple:
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, p: tuple) -> tuple:
    q = _ZERO
    while s > 0:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _equal(p: tuple, q: tuple) -> bool:
    return ((p[0] * q[2] - q[0] * p[2]) % _P == 0
            and (p[1] * q[2] - q[1] * p[2]) % _P == 0)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0) or 0
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _decompress(s: bytes) -> tuple | None:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _compress(p: tuple) -> bytes:
    zinv = _inv(p[2])
    x = p[0] * zinv % _P
    y = p[1] * zinv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _small_order(p: tuple) -> bool:
    q = _add(p, p)
    q = _add(q, q)
    q = _add(q, q)
    return _equal(q, _ZERO)


def public_key(seed: bytes) -> bytes:
    """The public key of a 32-byte secret seed (for tests and tooling)."""
    a, _ = _expand(seed)
    return _compress(_mul(a, _G))


def _expand(seed: bytes) -> tuple[int, bytes]:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def sign(seed: bytes, message: bytes) -> bytes:
    """An RFC 8032 signature. anirss itself never signs; this is here so the
    tests can build repos, and so a repo maintainer has a reference."""
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, _G))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L
    big_r = _compress(_mul(r, _G))
    k = int.from_bytes(hashlib.sha512(big_r + pub + message).digest(), "little") % _L
    return big_r + ((r + k * a) % _L).to_bytes(32, "little")


def verify(key: bytes, message: bytes, signature: bytes) -> bool:
    """Whether `signature` is `key`'s strict Ed25519 signature over `message`."""
    if len(key) != 32 or len(signature) != 64:
        return False
    a = _decompress(key)
    r = _decompress(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L or _small_order(a) or _small_order(r):
        return False
    k = int.from_bytes(hashlib.sha512(signature[:32] + key + message).digest(),
                       "little") % _L
    neg_a = ((_P - a[0]) % _P, a[1], a[2], (_P - a[3]) % _P)
    return _compress(_add(_mul(s, _G), _mul(k, neg_a))) == signature[:32]
