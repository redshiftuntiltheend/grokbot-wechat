"""Outbound WeChat image send (one image, no caption, no text-outbox fallback).

Protocol shape follows Tencent's public openclaw-weixin client, not a private
extension of this bridge:
  docs/protocol_zh_CN.md
  src/messaging/send.ts          (sendImageMessageWeixin)
  src/cdn/upload.ts              (getUploadUrl fields, no_need_thumb)
  src/cdn/cdn-upload.ts          (POST ciphertext, x-encrypted-param, retry)
  src/cdn/aes-ecb.ts             (AES-128-ECB, PKCS#7 size)
  src/cdn/cdn-url.ts             (fallback upload URL)

CDN POSTs must not carry the bot bearer token. API calls (getuploadurl /
sendmessage) go through ilink.post, which does. This module does not log
keys, tokens, upload parameters, or ciphertext. Failures never enter
outbox.json. Ambiguous sendmessage results are not retried.
"""
import base64, errno, fcntl, hashlib, json, os, socket, stat, time, urllib.error, urllib.parse, urllib.request
import secrets as _secrets
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import ilink, outbox

CDN_DEFAULT = "https://novac2c.cdn.weixin.qq.com/c2c"
CDN_DEFAULT_HOST = "novac2c.cdn.weixin.qq.com"
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # local guard, not a documented WeChat limit
UPLOAD_ATTEMPTS = 3
BLOCKING_STATES = ("send_started", "sent", "ambiguous", "rejected")
ALLOWED_EXT = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".gif": "gif", ".webp": "webp"}
PROTECTED_NAMES = {
    "credentials.json", ".env", "state.json", "outbox.json", "pending.json",
    "history.log", "forward.log", "forward.out", "qr_url.txt", "verify_code.txt",
    "image_send_ledger.json", "image_send_ledger.lock", "secrets.json",
}

class ImageSendError(Exception):
    def __init__(self, kind, message, exit_code):
        self.kind = kind
        self.exit_code = exit_code
        super().__init__(message)

def outbound_image_enabled():
    v = os.environ.get("WECHAT_OUTBOUND_IMAGE", "1").strip().lower()
    return v not in ("0", "false", "no", "off", "disabled")

def padded_size(n):
    """AES-128-ECB PKCS#7 ciphertext length. Matches aesEcbPaddedSize in aes-ecb.ts."""
    if n < 0:
        raise ImageSendError("validation", "image size is invalid", 1)
    return ((n + 16) // 16) * 16

def encrypt_aes_ecb(plaintext, key):
    """AES-128-ECB with PKCS#7. cryptography's ECB mode does not pad by itself."""
    if len(key) != 16:
        raise ImageSendError("upload", "AES key length is invalid", 1)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    out = enc.update(padded) + enc.finalize()
    if len(out) != padded_size(len(plaintext)):
        raise ImageSendError("upload", "ciphertext size does not match PKCS#7", 1)
    return out

def aes_key_for_message(key):
    """Base64 of the hex key string, which is what sendImageMessageWeixin puts in media.aes_key."""
    return base64.b64encode(key.hex().encode("ascii")).decode("ascii")

def is_superseded(reply_to_ms):
    """Same predicate as send.py: last_inbound_ms > reply_to_ms + 500."""
    if reply_to_ms is None:
        return False
    last = outbox.last_inbound_s()
    return bool(last and last * 1000 > reply_to_ms + 500)

def js_encode_uri_component(value):
    """Match JavaScript encodeURIComponent used by buildCdnUploadUrl."""
    return urllib.parse.quote(str(value), safe="-_.!~*'()")

def _host_ok(host):
    if not host or len(host) > 253 or host.startswith(".") or host.endswith("."):
        return False
    labels = host.split(".")
    if any(not lab or len(lab) > 63 or lab.startswith("-") or lab.endswith("-") for lab in labels):
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    return all(set(lab) <= allowed for lab in labels)

def allowed_cdn_hosts():
    hosts = {CDN_DEFAULT_HOST}
    extra = os.environ.get("WECHAT_CDN_HOST_ALLOWLIST", "")
    for part in extra.split(","):
        item = part.strip().lower().rstrip(".")
        if not item:
            continue
        if not _host_ok(item):
            raise ImageSendError("upload", "CDN host allowlist entry is invalid", 1)
        hosts.add(item)
    return hosts

def validate_cdn_url(url):
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise ImageSendError("upload", "CDN URL is not allowed", 1)
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        raise ImageSendError("upload", "CDN URL is not allowed", 1)
    if host not in allowed_cdn_hosts():
        raise ImageSendError("upload", "CDN URL host is not allowed", 1)
    return url.strip()

def cdn_base():
    raw = os.environ.get("WECHAT_CDN_BASE", "").strip() or CDN_DEFAULT
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme != "https" or parts.username or parts.password or parts.query or parts.fragment:
        raise ImageSendError("upload", "CDN base URL is not allowed", 1)
    if parts.port not in (None, 443):
        raise ImageSendError("upload", "CDN base URL is not allowed", 1)
    host = (parts.hostname or "").lower().rstrip(".")
    if host not in allowed_cdn_hosts():
        raise ImageSendError("upload", "CDN base URL host is not allowed", 1)
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit(("https", parts.netloc, path, "", ""))

def build_upload_url(upload_param, filekey):
    base = cdn_base()
    url = "%s/upload?encrypted_query_param=%s&filekey=%s" % (
        base, js_encode_uri_component(upload_param), js_encode_uri_component(filekey))
    return validate_cdn_url(url)

def select_upload_url(resp, filekey):
    if not isinstance(resp, dict):
        raise ImageSendError("upload", "getUploadUrl returned an unexpected response", 1)
    full = resp.get("upload_full_url")
    param = resp.get("upload_param")
    full = full.strip() if isinstance(full, str) else ""
    param = param.strip() if isinstance(param, str) else ""
    if full:
        return validate_cdn_url(full)
    if param:
        return build_upload_url(param, filekey)
    raise ImageSendError("upload", "getUploadUrl returned no upload URL", 1)

def build_upload_body(to, filekey, rawsize, rawfilemd5, filesize, aeskey_hex):
    return {
        "filekey": filekey,
        "media_type": 1,
        "to_user_id": to,
        "rawsize": rawsize,
        "rawfilemd5": rawfilemd5,
        "filesize": filesize,
        "no_need_thumb": True,
        "aeskey": aeskey_hex,
    }

def build_image_message(to, client_id, download_param, aes_key_b64, ciphertext_len, context_token):
    msg = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": 2,
        "message_state": 2,
        "item_list": [{
            "type": 2,
            "image_item": {
                "media": {
                    "encrypt_query_param": download_param,
                    "aes_key": aes_key_b64,
                    "encrypt_type": 1,
                },
                "mid_size": ciphertext_len,
            },
        }],
    }
    if context_token:
        msg["context_token"] = context_token
    return {"msg": msg}

def _magic_matches(kind, data):
    if kind == "jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if kind == "png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if kind == "gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if kind == "webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False

def _protected_inodes():
    found = set()
    roots = [ilink.DIR, os.path.dirname(os.path.abspath(__file__))]
    names = list(PROTECTED_NAMES) + [".env"]
    paths = [os.path.join(root, name) for root in roots for name in names]
    try:
        import config
        if config.SECRETS_FILE:
            paths.append(os.path.expanduser(config.SECRETS_FILE))
    except Exception:
        pass
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            found.add((st.st_dev, st.st_ino))
    return found

def _reject_protected(path, st):
    base = os.path.basename(os.path.realpath(path)).lower()
    if base in PROTECTED_NAMES or base == ".env" or base.startswith(".env.") or (
            base.endswith(".json") and "secret" in base):
        raise ImageSendError("validation", "refusing to send a protected file", 1)
    if (st.st_dev, st.st_ino) in _protected_inodes():
        raise ImageSendError("validation", "refusing to send a protected file", 1)

def read_image_file(path):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ImageSendError("validation", "image path is empty or invalid", 1)
    ext = os.path.splitext(path)[1].lower()
    kind = ALLOWED_EXT.get(ext)
    if not kind:
        raise ImageSendError("validation", "image extension is not allowed", 1)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno in (getattr(errno, "ELOOP", 40), getattr(errno, "EMLINK", 31)):
            raise ImageSendError("validation", "image path must not be a symlink", 1)
        raise ImageSendError("validation", "image file could not be opened", 1)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ImageSendError("validation", "image path is not a regular file", 1)
        if st.st_size <= 0:
            raise ImageSendError("validation", "image file is empty", 1)
        if st.st_size > MAX_IMAGE_BYTES:
            raise ImageSendError("validation", "image file exceeds the local size limit", 1)
        _reject_protected(path, st)
        buf = bytearray()
        remaining = st.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise ImageSendError("validation", "image file changed while reading", 1)
            if len(chunk) > remaining:
                raise ImageSendError("validation", "image file changed while reading", 1)
            buf += chunk
            remaining -= len(chunk)
        data = bytes(buf)
    finally:
        os.close(fd)
    if not _magic_matches(kind, data):
        raise ImageSendError("validation", "image contents do not match its extension", 1)
    # High-signal markers only. These strings are long enough that a real photo
    # almost never contains them by chance; this is not a general secret scanner.
    for marker in (b"bot_token", b"WECHAT_WEBHOOK_KEY", b"BEGIN OPENSSH PRIVATE KEY", b"BEGIN RSA PRIVATE KEY"):
        if marker in data:
            raise ImageSendError("validation", "refusing to send a protected file", 1)
    return data

def _num(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return "absent"
    return str(value)

def classify_send_response(raw):
    """sent / rejected / ambiguous. Missing ret matches the official sendMessage client.
    Empty or non-JSON bodies are ambiguous so the caller does not retry."""
    if raw is None:
        return "ambiguous", None, None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw.strip():
        return "ambiguous", None, None
    try:
        body = json.loads(raw)
    except ValueError:
        return "ambiguous", None, None
    if not isinstance(body, dict):
        return "ambiguous", None, None
    ret, err = body.get("ret", None), body.get("errcode", None)
    for value in (ret, err):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            return "ambiguous", ret if isinstance(ret, int) and not isinstance(ret, bool) else None, err if isinstance(err, int) and not isinstance(err, bool) else None
        if value != 0:
            return "rejected", ret, err
    return "sent", ret, err

def _header(headers, name):
    if not headers:
        return ""
    value = None
    if hasattr(headers, "get"):
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
    if value is None and hasattr(headers, "items"):
        for key, item in headers.items():
            if str(key).lower() == name.lower():
                value = item
                break
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return "" if value is None else str(value).strip()

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)

def _opener():
    return urllib.request.build_opener(_NoRedirect)

def build_cdn_request(url, ciphertext):
    """CDN POST carries only the ciphertext. No Authorization header is set or copied."""
    validate_cdn_url(url)
    if not isinstance(ciphertext, (bytes, bytearray)) or not ciphertext:
        raise ImageSendError("upload", "CDN body is empty", 1)
    req = urllib.request.Request(
        url, data=bytes(ciphertext), method="POST",
        headers={"Content-Type": "application/octet-stream", "User-Agent": "grokbot-wechat-image"})
    for key, value in req.header_items():
        if key.lower() in ("authorization", "authorizationtype") or "bearer " in str(value).lower():
            raise ImageSendError("upload", "CDN request must not carry authorization", 1)
    return req

def cdn_exchange(url, ciphertext, timeout, opener=None):
    req = build_cdn_request(url, ciphertext)
    opener = opener or _opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            return getattr(resp, "status", 200), getattr(resp, "headers", {}), resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read(4096)
        except Exception:
            body = b""
        return e.code, e.headers, body

def upload_ciphertext(url, ciphertext, exchange=None):
    exchange = exchange or cdn_exchange
    last = "CDN upload failed"
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            status, headers, _body = exchange(url, ciphertext, 60)
        except urllib.error.HTTPError as e:
            code = int(getattr(e, "code", 0) or 0)
            if 300 <= code < 400:
                raise ImageSendError("upload", "CDN redirect refused", 1) from None
            if 400 <= code < 500:
                raise ImageSendError("upload", "CDN upload rejected (HTTP %s)" % code, 1) from None
            last = "CDN upload failed"
            if attempt >= UPLOAD_ATTEMPTS:
                break
            continue
        except (TimeoutError, socket.timeout, urllib.error.URLError):
            last = "CDN upload failed"
            if attempt >= UPLOAD_ATTEMPTS:
                break
            continue
        if 300 <= int(status) < 400:
            raise ImageSendError("upload", "CDN redirect refused", 1)
        if 400 <= int(status) < 500:
            raise ImageSendError("upload", "CDN upload rejected (HTTP %s)" % int(status), 1)
        if int(status) != 200:
            last = "CDN upload failed"
            if attempt >= UPLOAD_ATTEMPTS:
                break
            continue
        param = _header(headers, "x-encrypted-param")
        if not param:
            last = "CDN upload response missing x-encrypted-param"
            if attempt >= UPLOAD_ATTEMPTS:
                break
            continue
        return param
    raise ImageSendError("upload", last, 1)

def default_api_post(endpoint, body, timeout):
    return ilink.post(endpoint, body, timeout=timeout)

def _parse_json_object(raw, label):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw.strip():
        raise ImageSendError("upload", "%s returned an unexpected response" % label, 1)
    try:
        body = json.loads(raw)
    except ValueError:
        raise ImageSendError("upload", "%s returned an unexpected response" % label, 1)
    if not isinstance(body, dict):
        raise ImageSendError("upload", "%s returned an unexpected response" % label, 1)
    return body

def ledger_path():
    return os.path.join(ilink.DIR, "image_send_ledger.json")

def _lock_path():
    return ledger_path() + ".lock"

def _read_ledger(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"version": 1, "items": {}}
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        return {"version": 1, "items": {}}
    return data

def _prune(items):
    if len(items) <= 500:
        return items
    droppable = [k for k, v in items.items()
                 if isinstance(v, dict) and v.get("state") not in ("send_started", "ambiguous")]
    droppable.sort(key=lambda k: items[k].get("updated_at") or 0)
    while len(items) > 500 and droppable:
        items.pop(droppable.pop(0), None)
    return items

def _write_ledger(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)

class _Ledger:
    def __init__(self):
        self._fh = open(_lock_path(), "a+")
        os.chmod(_lock_path(), 0o600)
        fcntl_lock(self._fh)
        self.path = ledger_path()
        self.data = _read_ledger(self.path)

    def get(self, key):
        item = self.data["items"].get(key)
        return item if isinstance(item, dict) else None

    def put(self, key, state, client_id, clock):
        self.data["items"][key] = {
            "state": state, "client_id": client_id, "updated_at": clock()}
        self.data["items"] = _prune(self.data["items"])
        _write_ledger(self.path, self.data)

    def close(self):
        try:
            fcntl_unlock(self._fh)
        finally:
            self._fh.close()

def fcntl_lock(fh):
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

def fcntl_unlock(fh):
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

def _idempotency_material(file_sha, to, reply_to_ms, idempotency_key):
    reply = "" if reply_to_ms is None else str(reply_to_ms)
    return hashlib.sha256(("\n".join([file_sha, to, reply, idempotency_key or ""])).encode()).hexdigest()

def _check_extra_key(idempotency_key):
    if not idempotency_key:
        return ""
    if not isinstance(idempotency_key, str) or len(idempotency_key) > 128:
        raise ImageSendError("validation", "idempotency key is invalid", 1)
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in idempotency_key):
        raise ImageSendError("validation", "idempotency key is invalid", 1)
    return idempotency_key

def resolve_recipient(to, context_token, creds_fn=None):
    """Images go only to the bound ilink_user_id. --to must match; last_from is not used."""
    creds_fn = creds_fn or ilink.creds
    try:
        creds = creds_fn()
    except ImageSendError:
        raise
    except Exception:
        raise ImageSendError("validation", "credentials are unavailable", 1) from None
    bound = creds.get("ilink_user_id") if isinstance(creds, dict) else None
    if not isinstance(bound, str) or not bound or any(c in bound for c in "\r\n\x00"):
        raise ImageSendError("validation", "credentials have no bound user", 1)
    if to is not None and to != bound:
        raise ImageSendError("validation", "image recipient must be the bound WeChat user", 1)
    st = ilink.load_state()
    tokens = st.get("context_tokens") if isinstance(st, dict) else None
    if not isinstance(tokens, dict):
        tokens = {}
    if context_token is None:
        context_token = tokens.get(bound) or ""
    if not isinstance(context_token, str) or any(c in context_token for c in "\r\n\x00"):
        raise ImageSendError("validation", "context token is invalid", 1)
    return bound, context_token

def send_image_file(path, to=None, context_token=None, reply_to_ms=None, idempotency_key="",
                    api_post=None, cdn_exchange_fn=None, superseded_fn=None, clock=None,
                    creds_fn=None, rng=None):
    """Send one local image. Raises ImageSendError. Does not touch the text outbox."""
    if not outbound_image_enabled():
        raise ImageSendError("disabled", "outbound image sending is disabled", 2)
    if reply_to_ms is not None and (isinstance(reply_to_ms, bool) or not isinstance(reply_to_ms, int)):
        raise ImageSendError("validation", "reply-to-ms must be an integer", 1)
    idempotency_key = _check_extra_key(idempotency_key)
    plaintext = read_image_file(path)
    check = superseded_fn or (lambda: is_superseded(reply_to_ms))
    if check():
        raise ImageSendError("superseded", "a newer inbound message superseded this reply", 4)
    to, context_token = resolve_recipient(to, context_token, creds_fn=creds_fn)
    if check():
        raise ImageSendError("superseded", "a newer inbound message superseded this reply", 4)

    api_post = api_post or default_api_post
    exchange = cdn_exchange_fn or cdn_exchange
    clock = clock or time.time
    rng = rng or _secrets
    file_sha = hashlib.sha256(plaintext).hexdigest()
    key_id = _idempotency_material(file_sha, to, reply_to_ms, idempotency_key)
    ledger = _Ledger()
    try:
        prior = ledger.get(key_id)
        if prior and prior.get("state") in BLOCKING_STATES:
            raise ImageSendError(
                "duplicate",
                "previous image send outcome was %s; not retrying" % prior.get("state"),
                5)
        if check():
            raise ImageSendError("superseded", "a newer inbound message superseded this reply", 4)

        aeskey = rng.token_bytes(16)
        filekey = rng.token_hex(16)
        aes_hex = aeskey.hex()
        ciphertext = encrypt_aes_ecb(plaintext, aeskey)
        upload_body = build_upload_body(
            to, filekey, len(plaintext), hashlib.md5(plaintext, usedforsecurity=False).hexdigest(), len(ciphertext), aes_hex)
        try:
            upload_raw = api_post("ilink/bot/getuploadurl", upload_body, 20)
        except ImageSendError:
            raise
        except Exception:
            ledger.put(key_id, "upload_failed", "", clock)
            raise ImageSendError("upload", "getUploadUrl failed", 1) from None
        try:
            upload_resp = _parse_json_object(upload_raw, "getUploadUrl")
            cdn_url = select_upload_url(upload_resp, filekey)
            download_param = upload_ciphertext(cdn_url, ciphertext, exchange=exchange)
        except ImageSendError as e:
            if e.kind == "upload":
                ledger.put(key_id, "upload_failed", "", clock)
            raise
        if check():
            raise ImageSendError("superseded", "a newer inbound message superseded this reply", 4)

        client_id = "openclaw-weixin-" + rng.token_hex(8)
        payload = build_image_message(
            to, client_id, download_param, aes_key_for_message(aeskey), len(ciphertext), context_token)
        # Persist before the request so a crash mid-send cannot be retried blindly.
        ledger.put(key_id, "send_started", client_id, clock)
        try:
            send_raw = api_post("ilink/bot/sendmessage", payload, 20)
        except Exception:
            ledger.put(key_id, "ambiguous", client_id, clock)
            raise ImageSendError("ambiguous", "send outcome is unknown; not retrying", 5) from None
        outcome, ret, err = classify_send_response(send_raw)
        if outcome == "sent":
            ledger.put(key_id, "sent", client_id, clock)
            try:
                import history
                history.append(history.grok_line("[图片]"))
            except Exception:
                pass
            return {"outcome": "sent", "client_id": client_id, "to": to}
        if outcome == "rejected":
            ledger.put(key_id, "rejected", client_id, clock)
            raise ImageSendError(
                "rejected",
                "send was rejected (ret=%s errcode=%s); not retrying" % (_num(ret), _num(err)),
                5)
        ledger.put(key_id, "ambiguous", client_id, clock)
        raise ImageSendError("ambiguous", "send outcome is unknown; not retrying", 5)
    finally:
        ledger.close()
