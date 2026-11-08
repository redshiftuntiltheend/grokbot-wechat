"""Offline tests for outbound image send. No WeChat, no CDN, no webhook.

Run via: python3 -m unittest discover -s tests -v
This file is named so it imports after tests/test_offline.py (which sets BRIDGE_DATA_DIR).
"""
import base64, io, json, os, stat, sys, tempfile, traceback, unittest, urllib.error, urllib.request
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "config" not in sys.modules:
    os.environ["BRIDGE_DATA_DIR"] = tempfile.mkdtemp(prefix="bridge-img-")
    os.environ.setdefault("OWNER_NAME", "Tester")
sys.path.insert(0, ROOT)

import history, ilink, image_send, media, outbox  # noqa: E402
import send_image  # noqa: E402

JPEG = b"\xff\xd8\xff" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8
FULL = "https://novac2c.cdn.weixin.qq.com/c2c/upload?uploadid=fixed"
OWNER = "owner@im.wechat"


class FixedRng:
    def __init__(self):
        self.hex_n = 0

    def token_bytes(self, n):
        return b"\x11" * n

    def token_hex(self, n):
        self.hex_n += 1
        return ("aa" if self.hex_n == 1 else "bb") * n


class DummyResp:
    def __init__(self, status, headers, body=b""):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self, n=-1):
        if n is None or n < 0 or n >= len(self._body):
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:n], self._body[n:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ScriptedOpener:
    def __init__(self, results):
        self.results = list(results)
        self.reqs = []

    def open(self, req, timeout=None):
        self.reqs.append(req)
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        status, headers, body = item
        return DummyResp(status, headers, body)


def creds():
    return {"bot_token": "test-bot-token", "ilink_user_id": OWNER,
            "baseurl": "https://ilinkai.weixin.qq.com"}


class ImageSendTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="img-", dir=ilink.DIR)
        self.path = os.path.join(self.dir, "pic.jpg")
        with open(self.path, "wb") as f:
            f.write(JPEG)
        self._env = {}
        for name in ("WECHAT_OUTBOUND_IMAGE", "WECHAT_CDN_HOST_ALLOWLIST", "WECHAT_CDN_BASE"):
            self._env[name] = os.environ.pop(name, None)
        for p in (image_send.ledger_path(), image_send._lock_path(), image_send.ledger_path() + ".tmp",
                  outbox.OUTBOX, history.HISTORY):
            if os.path.exists(p):
                os.remove(p)
        ilink.save_state({"last_inbound_ms": 1000, "context_tokens": {OWNER: "ctx-ok"},
                          "last_from_user_id": "someone-else@im.wechat"})
        self._enqueue, self._send_or_queue, self._send_text = outbox.enqueue, outbox.send_or_queue, outbox.send_text

    def tearDown(self):
        outbox.enqueue, outbox.send_or_queue, outbox.send_text = self._enqueue, self._send_or_queue, self._send_text
        for name, old in self._env.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def forbid_outbox(self):
        def boom(*args, **kwargs):
            raise AssertionError("text outbox was used")
        outbox.enqueue = boom
        outbox.send_or_queue = boom

    def send(self, api, exchange=None, path=None, **kw):
        self.forbid_outbox()
        kw.setdefault("creds_fn", creds)
        kw.setdefault("rng", FixedRng())
        kw.setdefault("context_token", "")
        if exchange is not None:
            kw["cdn_exchange_fn"] = exchange
        return image_send.send_image_file(path or self.path, api_post=api, **kw)

    def test_padding_matches_pkcs7_and_official_formula(self):
        key = bytes(range(16))
        for n in range(0, 65):
            official = ((n + 16) // 16) * 16  # ceil((n+1)/16)*16
            self.assertEqual(image_send.padded_size(n), official)
            blob = b"x" * n
            enc = image_send.encrypt_aes_ecb(blob, key)
            self.assertEqual(len(enc), official)
            self.assertNotEqual(enc, blob)
            pad = 16 - (n % 16) if n % 16 else 16
            manual = blob + bytes([pad]) * pad
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
            self.assertEqual(enc, e.update(manual) + e.finalize())
            self.assertEqual(media.decrypt(enc, key), blob)

    def test_message_key_is_base64_of_hex_not_raw(self):
        key = b"\x11" * 16
        b64 = image_send.aes_key_for_message(key)
        decoded = base64.b64decode(b64)
        self.assertEqual(decoded, key.hex().encode("ascii"))
        self.assertNotEqual(decoded, key)
        self.assertEqual(media.parse_key({"media": {"aes_key": b64}}), key)
        msg = image_send.build_image_message(OWNER, "openclaw-weixin-1", "param", b64, 48, "ctx")
        item = msg["msg"]["item_list"][0]
        self.assertEqual(item["type"], 2)
        self.assertEqual(item["image_item"]["media"]["encrypt_type"], 1)
        self.assertEqual(item["image_item"]["media"]["encrypt_query_param"], "param")
        self.assertEqual(item["image_item"]["mid_size"], 48)
        self.assertEqual(msg["msg"]["message_type"], 2)
        self.assertEqual(msg["msg"]["message_state"], 2)
        self.assertEqual(msg["msg"]["from_user_id"], "")
        self.assertEqual(msg["msg"]["context_token"], "ctx")
        self.assertNotIn("text_item", item)
        bare = image_send.build_image_message(OWNER, "c", "p", b64, 16, "")
        self.assertNotIn("context_token", bare["msg"])
        self.assertNotIn(key.hex(), json.dumps(msg))

    def test_success_structure_cdn_has_no_bearer_and_ledger_has_no_key(self):
        calls, cdn = [], []

        def api(endpoint, body, timeout):
            calls.append((endpoint, body, timeout))
            if endpoint == "ilink/bot/getuploadurl":
                return json.dumps({"upload_full_url": FULL, "upload_param": "do-not-use", "ret": 0})
            self.assertEqual(endpoint, "ilink/bot/sendmessage")
            return json.dumps({"ret": 0, "errmsg": ""})

        def exchange(url, data, timeout):
            cdn.append((url, bytes(data)))
            return 200, {"X-Encrypted-Param": "download-param"}, b""

        result = self.send(api, exchange, reply_to_ms=1000)
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(result["to"], OWNER)
        self.assertTrue(result["client_id"].startswith("openclaw-weixin-"))
        upload_ep, upload_body, _ = calls[0]
        self.assertEqual(upload_ep, "ilink/bot/getuploadurl")
        self.assertEqual(upload_body["media_type"], 1)
        self.assertEqual(upload_body["to_user_id"], OWNER)
        self.assertIs(upload_body["no_need_thumb"], True)
        self.assertEqual(upload_body["aeskey"], "11" * 16)
        self.assertEqual(len(upload_body["aeskey"]), 32)
        self.assertEqual(upload_body["rawsize"], len(JPEG))
        self.assertEqual(upload_body["filesize"], image_send.padded_size(len(JPEG)))
        self.assertNotIn("thumb_rawsize", upload_body)
        self.assertEqual(upload_body["rawfilemd5"], __import__("hashlib").md5(JPEG).hexdigest())
        self.assertEqual(cdn[0][0], FULL)
        self.assertNotIn("do-not-use", cdn[0][0])
        self.assertNotEqual(cdn[0][1], JPEG)
        self.assertEqual(media.decrypt(cdn[0][1], b"\x11" * 16), JPEG)
        sent = calls[1][1]["msg"]
        self.assertEqual(sent["to_user_id"], OWNER)
        self.assertNotEqual(sent["to_user_id"], "someone-else@im.wechat")
        self.assertNotIn("context_token", sent)
        media_obj = sent["item_list"][0]["image_item"]["media"]
        self.assertEqual(media_obj["encrypt_query_param"], "download-param")
        self.assertEqual(base64.b64decode(media_obj["aes_key"]), b"11" * 16)
        self.assertEqual(sent["item_list"][0]["image_item"]["mid_size"], len(cdn[0][1]))
        self.assertEqual(len(sent["item_list"]), 1)
        ledger = open(image_send.ledger_path(), encoding="utf-8").read()
        self.assertNotIn("11" * 16, ledger)
        self.assertNotIn("download-param", ledger)
        self.assertNotIn("test-bot-token", ledger)
        self.assertEqual(stat.S_IMODE(os.stat(image_send.ledger_path()).st_mode), 0o600)
        self.assertIn("[图片]", open(history.HISTORY, encoding="utf-8").read())
        self.assertFalse(os.path.exists(outbox.OUTBOX))

    def test_context_token_is_not_taken_from_another_user(self):
        seen = {}

        def api(endpoint, body, timeout):
            seen["to"] = body.get("to_user_id") or body["msg"]["to_user_id"]
            seen["body"] = body
            if endpoint.endswith("getuploadurl"):
                return json.dumps({"upload_param": "abc"})
            return "{}"

        def exchange(url, data, timeout):
            self.assertIn("encrypted_query_param=abc", url)
            self.assertIn("filekey=", url)
            self.assertTrue(url.startswith("https://novac2c.cdn.weixin.qq.com/c2c/upload?"))
            return 200, {"x-encrypted-param": "p"}, b""

        self.send(api, exchange, context_token=None)
        self.assertEqual(seen["body"]["msg"]["context_token"], "ctx-ok")
        self.assertNotIn("someone-else", json.dumps(seen["body"]))

    def test_fallback_url_encodes_like_encodeURIComponent(self):
        param = "a+b/c=!~*'()"
        url = image_send.build_upload_url(param, "abc")
        self.assertIn("encrypted_query_param=a%2Bb%2Fc%3D!~*'()", url)
        self.assertIn("filekey=abc", url)
        self.assertNotIn(param, url.split("?", 1)[1].split("a%2B", 1)[0])

    def test_missing_upload_metadata_does_not_touch_cdn_or_outbox(self):
        cdn = []

        def api(endpoint, body, timeout):
            return json.dumps({"ret": 0, "errmsg": "no url"})

        def exchange(url, data, timeout):
            cdn.append(url)
            return 200, {"x-encrypted-param": "x"}, b""

        with self.assertRaises(image_send.ImageSendError) as cm:
            self.send(api, exchange)
        self.assertEqual(cm.exception.exit_code, 1)
        self.assertEqual(cdn, [])
        self.assertFalse(os.path.exists(outbox.OUTBOX))
        self.assertNotIn("no url", str(cm.exception))

    def test_cdn_4xx_does_not_retry_or_send(self):
        calls = []

        def api(endpoint, body, timeout):
            calls.append(endpoint)
            if endpoint.endswith("sendmessage"):
                raise AssertionError("sendmessage")
            return json.dumps({"upload_full_url": FULL})

        n = {"n": 0}

        def exchange(url, data, timeout):
            n["n"] += 1
            return 400, {"x-error-message": "bad-token-leak"}, b"body-leak"

        with self.assertRaises(image_send.ImageSendError) as cm:
            self.send(api, exchange)
        self.assertEqual(n["n"], 1)
        self.assertEqual(calls, ["ilink/bot/getuploadurl"])
        self.assertNotIn("bad-token", str(cm.exception))
        self.assertNotIn("body-leak", str(cm.exception))
        self.assertIn("400", str(cm.exception))

    def test_cdn_5xx_retries_then_missing_header_fails_without_send(self):
        n = {"n": 0}

        def api(endpoint, body, timeout):
            if endpoint.endswith("sendmessage"):
                raise AssertionError("send")
            return json.dumps({"upload_full_url": FULL})

        def exchange(url, data, timeout):
            n["n"] += 1
            if n["n"] < 3:
                return 503, {}, b""
            return 200, {}, b""

        with self.assertRaises(image_send.ImageSendError) as cm:
            self.send(api, exchange)
        self.assertEqual(n["n"], 3)
        self.assertIn("x-encrypted-param", str(cm.exception))
        self.assertFalse(os.path.exists(outbox.OUTBOX))

    def test_cdn_5xx_then_success(self):
        n = {"n": 0}

        def api(endpoint, body, timeout):
            if endpoint.endswith("getuploadurl"):
                return json.dumps({"upload_full_url": FULL})
            return json.dumps({"ret": 0})

        def exchange(url, data, timeout):
            n["n"] += 1
            if n["n"] == 1:
                return 502, {}, b""
            return 200, {"x-encrypted-param": "ok"}, b""

        result = self.send(api, exchange)
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(n["n"], 2)

    def test_redirect_and_disallowed_host_do_not_send(self):
        def api_redirect(endpoint, body, timeout):
            return json.dumps({"upload_full_url": FULL})

        def exchange(url, data, timeout):
            return 302, {"Location": "https://evil.example/collect"}, b""

        with self.assertRaises(image_send.ImageSendError) as cm:
            self.send(api_redirect, exchange)
        self.assertNotIn("evil.example", str(cm.exception))
        req = urllib.request.Request(FULL, data=b"x", method="POST")
        with self.assertRaises(urllib.error.HTTPError):
            image_send._NoRedirect().redirect_request(req, None, 302, "moved", {}, "https://evil.example/stolen")

        def api_evil(endpoint, body, timeout):
            if endpoint.endswith("sendmessage"):
                raise AssertionError("sent")
            return json.dumps({"upload_full_url": "https://novac2c.cdn.weixin.qq.com.evil.example/c2c/upload"})

        with self.assertRaises(image_send.ImageSendError) as cm:
            self.send(api_evil, exchange)
        self.assertNotIn("evil", str(cm.exception))
        for bad in (
            "http://novac2c.cdn.weixin.qq.com/c2c/upload",
            "https://user:pass@novac2c.cdn.weixin.qq.com/c2c/upload",
            "https://127.0.0.1/upload",
            "file:///etc/passwd",
        ):
            with self.assertRaises(image_send.ImageSendError):
                image_send.validate_cdn_url(bad)

    def test_real_cdn_request_has_no_authorization_even_if_api_does(self):
        token = "bot-token-TEST-DO-NOT-LEAK"
        captured = []
        orig_urlopen, orig_creds = urllib.request.urlopen, ilink.creds

        def fake_urlopen(req, timeout=None):
            captured.append(req)

            class R:
                def read(self):
                    return b"{}"

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return R()

        urllib.request.urlopen = fake_urlopen
        ilink.creds = lambda: {"bot_token": token, "baseurl": "https://ilinkai.weixin.qq.com", "ilink_user_id": OWNER}
        try:
            ilink.post("ilink/bot/getuploadurl", {"filekey": "abc"})
            opener = ScriptedOpener([(200, {"x-encrypted-param": "enc"}, b"")])
            image_send.cdn_exchange(FULL, b"ciphertext-bytes", 5, opener=opener)
        finally:
            urllib.request.urlopen, ilink.creds = orig_urlopen, orig_creds
        api_headers = dict((k.lower(), v) for k, v in captured[0].header_items())
        self.assertIn(token, api_headers.get("authorization", ""))
        cdn_req = opener.reqs[0]
        cdn_headers = dict((k.lower(), v) for k, v in cdn_req.header_items())
        self.assertNotIn("authorization", cdn_headers)
        self.assertNotIn("authorizationtype", cdn_headers)
        joined = repr(cdn_req.header_items()) + repr(cdn_req.data)
        self.assertNotIn(token, joined)
        self.assertNotIn("Bearer", joined)
        self.assertEqual(cdn_req.get_method(), "POST")
        self.assertEqual(cdn_req.data, b"ciphertext-bytes")
        self.assertIn("application/octet-stream", cdn_headers["content-type"])

    def test_superseded_before_upload_skips_credentials_and_network(self):
        def no_creds():
            raise AssertionError("creds")

        def api(*args, **kwargs):
            raise AssertionError("api")

        with self.assertRaises(image_send.ImageSendError) as cm:
            self.send(api, creds_fn=no_creds, superseded_fn=lambda: True, reply_to_ms=1)
        self.assertEqual(cm.exception.exit_code, 4)
        self.assertFalse(os.path.exists(outbox.OUTBOX))

    def test_superseded_after_upload_does_not_send(self):
        state = {"uploaded": False}
        calls = []

        def api(endpoint, body, timeout):
            calls.append(endpoint)
            if endpoint.endswith("sendmessage"):
                raise AssertionError("sent after supersession")
            return json.dumps({"upload_full_url": FULL})

        def exchange(url, data, timeout):
            state["uploaded"] = True
            return 200, {"x-encrypted-param": "p"}, b""

        def check():
            return state["uploaded"]

        with self.assertRaises(image_send.ImageSendError) as cm:
            self.send(api, exchange, superseded_fn=check, reply_to_ms=1)
        self.assertEqual(cm.exception.exit_code, 4)
        self.assertEqual(calls, ["ilink/bot/getuploadurl"])
        self.assertTrue(state["uploaded"])

    def test_supersession_predicate_matches_send_py(self):
        ilink.save_state({"last_inbound_ms": 10000})
        last = outbox.last_inbound_s()
        for reply in (None, 9499, 9500, 10000):
            expected = bool(reply is not None and last and last * 1000 > reply + 500)
            self.assertEqual(image_send.is_superseded(reply), expected)
        self.assertTrue(image_send.is_superseded(9499))
        self.assertFalse(image_send.is_superseded(9500))

    def test_ambiguous_send_is_not_retried_and_blocks_duplicate(self):
        secret = "SECRET-TIMEOUT-TOKEN"
        sends = {"n": 0}

        def api(endpoint, body, timeout):
            if endpoint.endswith("getuploadurl"):
                return json.dumps({"upload_full_url": FULL})
            sends["n"] += 1
            raise TimeoutError("timed out " + secret)

        def exchange(url, data, timeout):
            return 200, {"x-encrypted-param": "p"}, b""

        with self.assertRaises(image_send.ImageSendError) as cm:
            self.send(api, exchange, reply_to_ms=5000)
        self.assertEqual(cm.exception.exit_code, 5)
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__suppress_context__)
        self.assertNotIn(secret, str(cm.exception))
        self.assertNotIn(secret, "".join(traceback.format_exception(cm.exception)))
        self.assertEqual(sends["n"], 1)

        def api_again(endpoint, body, timeout):
            raise AssertionError("retried " + endpoint)

        with self.assertRaises(image_send.ImageSendError) as cm2:
            self.send(api_again, exchange, reply_to_ms=5000)
        self.assertEqual(cm2.exception.kind, "duplicate")
        self.assertIn("ambiguous", str(cm2.exception))
        self.assertFalse(os.path.exists(outbox.OUTBOX))
        self.assertFalse(os.path.exists(history.HISTORY))

    def test_business_reject_and_non_json_do_not_enter_outbox_or_retry(self):
        def exchange(url, data, timeout):
            return 200, {"x-encrypted-param": "p"}, b""

        def reject(endpoint, body, timeout):
            if endpoint.endswith("getuploadurl"):
                return json.dumps({"upload_full_url": FULL})
            return json.dumps({"ret": 12, "errcode": 0, "errmsg": "window"})

        with self.assertRaises(image_send.ImageSendError) as cm:
            self.send(reject, exchange, reply_to_ms=5000)
        self.assertEqual(cm.exception.exit_code, 5)
        self.assertIn("ret=12", str(cm.exception))
        self.assertNotIn("window", str(cm.exception))

        def should_not(endpoint, body, timeout):
            raise AssertionError(endpoint)

        with self.assertRaises(image_send.ImageSendError):
            self.send(should_not, exchange, reply_to_ms=5000)
        self.assertFalse(os.path.exists(outbox.OUTBOX))

        def garbage(endpoint, body, timeout):
            if endpoint.endswith("getuploadurl"):
                return json.dumps({"upload_full_url": FULL})
            return "not-json"
        with self.assertRaises(image_send.ImageSendError) as cm2:
            self.send(garbage, exchange, reply_to_ms=5001)
        self.assertEqual(cm2.exception.kind, "ambiguous")
        self.assertNotIn("not-json", str(cm2.exception))

    def test_empty_object_is_success_empty_string_is_ambiguous(self):
        self.assertEqual(image_send.classify_send_response("{}")[0], "sent")
        self.assertEqual(image_send.classify_send_response('{"ret":0}')[0], "sent")
        self.assertEqual(image_send.classify_send_response("")[0], "ambiguous")
        self.assertEqual(image_send.classify_send_response("   ")[0], "ambiguous")
        self.assertEqual(image_send.classify_send_response('{"ret":"0"}')[0], "ambiguous")
        self.assertEqual(image_send.classify_send_response('{"errcode":1}')[0], "rejected")
        self.assertEqual(image_send.classify_send_response("[1]")[0], "ambiguous")

    def test_upload_failure_can_be_retried_and_does_not_queue_text(self):
        secret = "UPLOAD-SECRET"
        phase = {"ok": False}

        def api(endpoint, body, timeout):
            if not phase["ok"]:
                raise RuntimeError(secret)
            if endpoint.endswith("getuploadurl"):
                return json.dumps({"upload_full_url": FULL})
            return json.dumps({"ret": 0})

        def exchange(url, data, timeout):
            return 200, {"x-encrypted-param": "p"}, b""

        with self.assertRaises(image_send.ImageSendError) as cm:
            self.send(api, exchange, reply_to_ms=5000)
        self.assertEqual(cm.exception.exit_code, 1)
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__suppress_context__)
        self.assertNotIn(secret, str(cm.exception))
        self.assertNotIn(secret, "".join(traceback.format_exception(cm.exception)))
        phase["ok"] = True
        result = self.send(api, exchange, reply_to_ms=5000)
        self.assertEqual(result["outcome"], "sent")
        self.assertFalse(os.path.exists(outbox.OUTBOX))

    def test_new_reply_or_idempotency_key_is_a_different_send(self):
        def api(endpoint, body, timeout):
            if endpoint.endswith("getuploadurl"):
                return json.dumps({"upload_full_url": FULL})
            return json.dumps({"ret": 0})

        def exchange(url, data, timeout):
            return 200, {"x-encrypted-param": "p"}, b""

        self.send(api, exchange, reply_to_ms=5000)
        again = self.send(api, exchange, reply_to_ms=5001)
        self.assertEqual(again["outcome"], "sent")
        with self.assertRaises(image_send.ImageSendError) as cm:
            self.send(api, exchange, reply_to_ms=5001)
        self.assertEqual(cm.exception.kind, "duplicate")
        third = self.send(api, exchange, reply_to_ms=5001, idempotency_key="second-copy")
        self.assertEqual(third["outcome"], "sent")

    def test_wrong_recipient_is_refused_before_upload(self):
        def api(*args, **kwargs):
            raise AssertionError("api")

        with self.assertRaises(image_send.ImageSendError) as cm:
            self.send(api, to="other@im.wechat")
        self.assertIn("bound", str(cm.exception))
        self.assertEqual(cm.exception.exit_code, 1)

    def test_validation(self):
        self.assertEqual(image_send.read_image_file(self.write("a.png", PNG))[:8], PNG[:8])
        self.assertTrue(image_send.read_image_file(self.write("a.gif", GIF)).startswith(b"GIF89a"))
        self.assertTrue(image_send.read_image_file(self.write("a.webp", WEBP)).startswith(b"RIFF"))
        with self.assertRaises(image_send.ImageSendError):
            image_send.read_image_file(self.write("bad.jpg", PNG))
        with self.assertRaises(image_send.ImageSendError):
            image_send.read_image_file(self.write("empty.jpg", b""))
        with self.assertRaises(image_send.ImageSendError):
            image_send.read_image_file(self.write("note.txt", JPEG))
        link = os.path.join(self.dir, "link.jpg")
        os.symlink(self.path, link)
        with self.assertRaises(image_send.ImageSendError) as cm:
            image_send.read_image_file(link)
        self.assertIn("symlink", str(cm.exception))
        cred = os.path.join(ilink.DIR, "credentials.json")
        with open(cred, "wb") as f:
            f.write(JPEG)
        linked = os.path.join(self.dir, "hard.jpg")
        os.link(cred, linked)
        with self.assertRaises(image_send.ImageSendError) as cm:
            image_send.read_image_file(linked)
        self.assertIn("protected", str(cm.exception))
        poisoned = JPEG + b"\nbot_token=nope"
        with self.assertRaises(image_send.ImageSendError):
            image_send.read_image_file(self.write("poison.jpg", poisoned))
        old = image_send.MAX_IMAGE_BYTES
        image_send.MAX_IMAGE_BYTES = 8
        try:
            with self.assertRaises(image_send.ImageSendError) as cm:
                image_send.read_image_file(self.path)
            self.assertIn("size", str(cm.exception))
        finally:
            image_send.MAX_IMAGE_BYTES = old
        with self.assertRaises(image_send.ImageSendError):
            image_send.read_image_file(self.dir + ".jpg" if False else self.dir)

    def test_directory_rejected(self):
        os.mkdir(os.path.join(self.dir, "dir.jpg"))
        with self.assertRaises(image_send.ImageSendError):
            image_send.read_image_file(os.path.join(self.dir, "dir.jpg"))

    def test_disable_switch_does_not_affect_text_outbox(self):
        os.environ["WECHAT_OUTBOUND_IMAGE"] = "off"

        def api(*args, **kwargs):
            raise AssertionError("api")

        with self.assertRaises(image_send.ImageSendError) as cm:
            image_send.send_image_file(self.path, creds_fn=creds, api_post=api)
        self.assertEqual(cm.exception.exit_code, 2)
        outbox.send_text = lambda text, **kw: ('{"ret": 9}', False)
        if os.path.exists(outbox.OUTBOX):
            os.remove(outbox.OUTBOX)
        sent, _resp = outbox.send_or_queue("text-still-queues", to=OWNER)
        self.assertFalse(sent)
        queued = json.load(open(outbox.OUTBOX, encoding="utf-8"))
        self.assertEqual(queued[-1]["text"], "text-still-queues")
        os.environ["WECHAT_OUTBOUND_IMAGE"] = "0"

        def api_fail(endpoint, body, timeout):
            raise RuntimeError("image failed")

        with self.assertRaises(image_send.ImageSendError):
            image_send.send_image_file(self.path, creds_fn=creds, api_post=api_fail, context_token="")
        queued_after = json.load(open(outbox.OUTBOX, encoding="utf-8"))
        self.assertEqual(queued_after, queued)

    def test_text_command_source_is_unchanged_and_unwired(self):
        for name in ("send.py", "forward.py", "poll.py", "bind.py", "outbox.py", "ilink.py",
                     "run_forward.sh", "watchdog.sh"):
            text = open(os.path.join(ROOT, name), encoding="utf-8").read()
            self.assertNotIn("image_send", text)
            self.assertNotIn("send_image", text)
        send_py = open(os.path.join(ROOT, "send.py"), encoding="utf-8").read()
        self.assertIn("outbox.send_or_queue", send_py)
        self.assertIn("reply_to_ms + 500", send_py)

    def test_cli_prints_safe_line(self):
        def fake(path, **kw):
            raise image_send.ImageSendError("ambiguous", "send outcome is unknown; not retrying", 5)

        orig = image_send.send_image_file
        image_send.send_image_file = fake
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = send_image.main([self.path, "--reply-to-ms", "5", "--no-context"])
        finally:
            image_send.send_image_file = orig
        self.assertEqual(code, 5)
        self.assertIn("DO NOT RETRY", buf.getvalue())
        self.assertNotIn("Bearer", buf.getvalue())

    def test_allowlist_accepts_exact_extra_host_only(self):
        os.environ["WECHAT_CDN_HOST_ALLOWLIST"] = "cdn.example.weixin.qq.com"
        image_send.validate_cdn_url("https://cdn.example.weixin.qq.com/c2c/upload")
        with self.assertRaises(image_send.ImageSendError):
            image_send.validate_cdn_url("https://cdn.example.weixin.qq.com.attacker.test/upload")
        with self.assertRaises(image_send.ImageSendError):
            os.environ["WECHAT_CDN_HOST_ALLOWLIST"] = "https://bad"
            image_send.allowed_cdn_hosts()


if __name__ == "__main__":
    unittest.main()
