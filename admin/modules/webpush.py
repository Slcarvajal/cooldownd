"""Web Push to the panel itself (RFC 8030 / 8291 / 8292): the iPhone gets alerts from the panel added
to its Home Screen, with no other app. Not a card; modules/notify.py uses it.

The VAPID key lives in ~/.config/homelab-admin/vapid.pem (600, made on first use) and the devices'
subscriptions in ~/.local/share/homelab-admin/push.json (600). Payloads are encrypted for each
device, so the push service (Apple's, Google's) carries only ciphertext. Needs `cryptography`
(Debian's python3-cryptography on the Pi), imported only here."""
import base64
import json
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

KEY_PATH = os.path.expanduser("~/.config/homelab-admin/vapid.pem")
SUBS_PATH = os.path.expanduser("~/.local/share/homelab-admin/push.json")
# only real browser push services: a subscription can't make the panel POST anywhere else
PUSH_HOSTS = ("web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com", ".notify.windows.com")
_lock = threading.Lock()


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _write_private(path, data, mode="wb"):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, mode) as f:
        f.write(data)
    os.replace(tmp, path)


def _key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        with open(KEY_PATH, "rb") as f:
            return serialization.load_pem_private_key(f.read(), None)
    except FileNotFoundError:
        key = ec.generate_private_key(ec.SECP256R1())
        _write_private(KEY_PATH, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
        return key


def _raw_public(key):
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key():
    """The applicationServerKey the page subscribes with (base64url)."""
    return _b64(_raw_public(_key()))


def subscriptions():
    try:
        with open(SUBS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save(subs):
    _write_private(SUBS_PATH, json.dumps(subs, indent=1), "w")


def add(sub, label):
    """Store a browser PushSubscription (its toJSON()); replaces the same endpoint."""
    endpoint = str(sub.get("endpoint", ""))
    keys = sub.get("keys") or {}
    host = urllib.parse.urlsplit(endpoint).hostname or ""
    if not endpoint.startswith("https://") or not any(host == h or (h[0] == "." and host.endswith(h)) for h in PUSH_HOSTS):
        raise ValueError("That isn't a browser push service.")
    if len(_unb64(keys.get("p256dh", ""))) != 65 or len(_unb64(keys.get("auth", ""))) != 16:
        raise ValueError("The subscription's keys are malformed.")
    with _lock:
        subs = [s for s in subscriptions() if s["endpoint"] != endpoint]
        subs.append({"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
                     "label": label[:60], "added": time.strftime("%Y-%m-%d %H:%M")})
        _save(subs)


def remove(endpoint):
    with _lock:
        _save([s for s in subscriptions() if s["endpoint"] != endpoint])


def encrypt(payload, p256dh, auth, salt=None, ephemeral=None):
    """RFC 8291 aes128gcm body for one device."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    def hkdf(salt, ikm, info, n):
        return HKDF(hashes.SHA256(), n, salt, info).derive(ikm)

    ua_public = _unb64(p256dh)
    as_key = ephemeral or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(as_key)
    secret = as_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = hkdf(_unb64(auth), secret, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # one record, no padding
    return salt + struct.pack("!IB", 4096, len(as_public)) + as_public + body


def _vapid(endpoint, subject):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    key = _key()
    parts = urllib.parse.urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": subject}
    signing = (_b64(json.dumps({"typ": "JWT", "alg": "ES256"}).encode()) + "." +
               _b64(json.dumps(claims, separators=(",", ":")).encode()))
    r, s = decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    jwt = signing + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={jwt}, k={_b64(_raw_public(key))}"


def send_all(message, subject, ttl=86400, urgency="high"):
    """Push a JSON message to every device. (delivered, errors). Devices the push service
    says are gone (404 / 410) are dropped."""
    data = json.dumps(message).encode()
    delivered, errors = 0, []
    for s in subscriptions():
        req = urllib.request.Request(s["endpoint"], data=encrypt(data, s["keys"]["p256dh"], s["keys"]["auth"]),
                                     method="POST", headers={
                                         "Authorization": _vapid(s["endpoint"], subject), "Content-Encoding": "aes128gcm",
                                         "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": urgency})
        try:
            with urllib.request.urlopen(req, timeout=10):
                delivered += 1
        except urllib.error.HTTPError as ex:
            if ex.code in (404, 410):
                remove(s["endpoint"])
                errors.append(f"{s['label']}: unsubscribed, removed")
            else:
                errors.append(f"{s['label']}: HTTP {ex.code} {ex.read(200).decode(errors='replace')}")
        except (OSError, urllib.error.URLError) as ex:
            errors.append(f"{s['label']}: {ex}")
    return delivered, errors
