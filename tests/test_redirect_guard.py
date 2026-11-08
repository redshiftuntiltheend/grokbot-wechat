"""Offline proof that API and webhook redirects do not leak secrets.

Loopback only. Fake tokens. No WeChat, CDN, or webhook host.
Run: python3 -m unittest tests.test_redirect_guard -v
"""
import json, os, sys, threading, types, unittest, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "config" not in sys.modules:
    import tempfile
    os.environ["BRIDGE_DATA_DIR"] = tempfile.mkdtemp(prefix="bridge-redir-")
    os.environ.setdefault("OWNER_NAME", "Tester")
sys.path.insert(0, ROOT)

import forward, ilink, image_send  # noqa: E402

BOT = "fake-bot-token-not-real"
WEBHOOK_KEY = "fake-webhook-key-not-real"
BODY_MARK = "PRIVATE-BODY-MARKER"
VERIFY = "verify-code-987654"
CODES = (301, 302, 303, 307, 308)


class Quiet(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _record(self, body):
        self.server.hits.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": body,
        })

    def do_POST(self):
        body = self._body()
        self._record(body)
        self._reply()

    do_GET = do_POST

    def _reply(self):
        mode = self.server.mode
        if mode.get("redirect"):
            self.send_response(mode["code"])
            self.send_header("Location", mode["location"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = mode.get("body", b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    httpd.hits = []
    httpd.mode = {}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class RedirectGuardTest(unittest.TestCase):
    def setUp(self):
        self.evil = _serve()
        self.origin = _serve()
        self.evil_url = "http://127.0.0.1:%d/sink" % self.evil.server_address[1]
        self.origin_base = "http://127.0.0.1:%d" % self.origin.server_address[1]
        self.addCleanup(self.origin.shutdown)
        self.addCleanup(self.evil.shutdown)
        cred = {"bot_token": BOT, "baseurl": self.origin_base, "ilink_user_id": "owner@im.wechat"}
        with open(ilink.CRED, "w") as f:
            json.dump(cred, f)
        os.chmod(ilink.CRED, 0o600)
        ilink.save_state({"context_tokens": {}})
        self._env_key = os.environ.get("WECHAT_WEBHOOK_KEY")
        os.environ["WECHAT_WEBHOOK_KEY"] = WEBHOOK_KEY
        self._webhook = forward.WEBHOOK_URL

    def tearDown(self):
        forward.WEBHOOK_URL = self._webhook
        if self._env_key is None:
            os.environ.pop("WECHAT_WEBHOOK_KEY", None)
        else:
            os.environ["WECHAT_WEBHOOK_KEY"] = self._env_key
        if os.path.exists(ilink.CRED):
            os.remove(ilink.CRED)

    def _clear(self):
        self.origin.hits = []
        self.evil.hits = []

    def _redirect(self, code):
        self._clear()
        self.origin.mode = {"redirect": True, "code": code, "location": self.evil_url}

    def _assert_sink_clean(self, err=None):
        self.assertEqual(self.evil.hits, [])
        blob = json.dumps(self.evil.hits) + ("" if err is None else str(err))
        for secret in (BOT, WEBHOOK_KEY, BODY_MARK, VERIFY, self.evil_url):
            self.assertNotIn(secret, blob)
        self.assertEqual(len(self.origin.hits), 1)

    def test_api_redirect_does_not_reach_other_host(self):
        for code in CODES:
            self._redirect(code)
            with self.subTest(code=code):
                with self.assertRaises(ilink.RedirectRefused) as cm:
                    ilink.post("ilink/bot/sendmessage", {"msg": {"text": BODY_MARK}})
                self.assertEqual(cm.exception.status, code)
                self.assertIsNone(cm.exception.__cause__)
                self._assert_sink_clean(cm.exception)
                self.assertIn(BOT, self.origin.hits[0]["authorization"])
                self.assertIn(BODY_MARK.encode(), self.origin.hits[0]["body"])

    def test_webhook_redirect_does_not_reach_other_host(self):
        forward.WEBHOOK_URL = self.origin_base + "/hook"
        for code in CODES:
            self._redirect(code)
            with self.subTest(code=code):
                result = forward.post_webhook({"texts": [BODY_MARK]}, tries=3)
                self.assertIsNone(result)
                self._assert_sink_clean()
                self.assertEqual(self.origin.hits[0]["authorization"], "Bearer " + WEBHOOK_KEY)
                self.assertIn(BODY_MARK.encode(), self.origin.hits[0]["body"])

    def test_bind_get_redirect_does_not_forward_verify_code(self):
        if "qrcode" not in sys.modules:
            try:
                import qrcode  # noqa: F401
            except ModuleNotFoundError:
                sys.modules["qrcode"] = types.ModuleType("qrcode")
        import bind
        for code in CODES:
            self._redirect(code)
            with self.subTest(code=code):
                url = self.origin_base + "/ilink/bot/get_qrcode_status?verify_code=" + VERIFY
                with self.assertRaises(ilink.RedirectRefused):
                    bind.req(url)
                self._assert_sink_clean()
                self.assertIn(VERIFY, self.origin.hits[0]["path"])
                self.assertIsNone(self.origin.hits[0]["authorization"])

    def test_direct_200_still_returns_body_for_text_and_webhook(self):
        self._clear()
        self.origin.mode = {"body": b'{"ret":0}'}
        resp, has_ctx = ilink.send_text("hello", to="owner@im.wechat", context_token="")
        self.assertEqual(resp.strip(), '{"ret":0}')
        self.assertFalse(has_ctx)
        self.assertEqual(self.evil.hits, [])
        self.assertIn(b"hello", self.origin.hits[0]["body"])
        self.assertIn(BOT, self.origin.hits[0]["authorization"])

        self._clear()
        self.origin.mode = {"body": b'{"ok":true}'}
        forward.WEBHOOK_URL = self.origin_base + "/hook"
        status, body = forward.post_webhook({"texts": ["hello-hook"]}, tries=1)
        self.assertEqual(status, 200)
        self.assertIn("ok", body)
        self.assertEqual(self.evil.hits, [])
        self.assertEqual(self.origin.hits[0]["authorization"], "Bearer " + WEBHOOK_KEY)

    def test_image_upload_redirect_handler_does_not_contact_sink(self):
        self.assertIs(image_send._NoRedirect, ilink.NoRedirect)
        self._redirect(302)
        opener = urllib.request.build_opener(image_send._NoRedirect)
        req = urllib.request.Request(self.origin_base + "/upload", data=b"CIPHERTEXT-" + BODY_MARK.encode(), method="POST")
        with self.assertRaises(ilink.RedirectRefused):
            ilink.urlopen_no_redirect(req, timeout=3)
        # The image class itself must raise before a second hop when used as an opener.
        req2 = urllib.request.Request(self.origin_base + "/upload", data=b"CIPHERTEXT-" + BODY_MARK.encode(), method="POST")
        with self.assertRaises(urllib.error.HTTPError):
            opener.open(req2, timeout=3)
        self.assertEqual(self.evil.hits, [])
        joined = b"".join(hit["body"] for hit in self.origin.hits)
        self.assertIn(BODY_MARK.encode(), joined)
        self.assertTrue(all(hit["authorization"] is None for hit in self.origin.hits))


if __name__ == "__main__":
    unittest.main()
