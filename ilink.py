"""Minimal WeChat ClawBot iLink client.
Protocol notes: https://chihao.li/blog/en/wechat-clawbot-ilink-protocol and the
@tencent-weixin/openclaw-weixin 2.4.9 npm package (headers / base_info mimic that client)."""
import base64, json, os, random, secrets, urllib.error, urllib.request
from config import DATA_DIR
DIR = DATA_DIR
CRED = os.path.join(DIR, "credentials.json")   # written by bind.py; NEVER commit
STATE = os.path.join(DIR, "state.json")        # cursor + context tokens + recent raw msgs; NEVER commit
BASE_INFO = {"channel_version": "2.4.9", "bot_agent": "OpenClaw"}

def creds():
    return json.load(open(CRED))

def load_state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {}

def save_state(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f: json.dump(s, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600); os.replace(tmp, STATE)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not follow HTTP redirects. urllib copies Authorization onto 301/302/303."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)

class RedirectRefused(Exception):
    def __init__(self, status):
        self.status = int(status)
        super().__init__("redirect refused (HTTP %s)" % self.status)

def urlopen_no_redirect(req, timeout):
    """Open req and refuse every 3xx. The error text does not include Location or secrets."""
    opener = urllib.request.build_opener(NoRedirect)
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        code = getattr(e, "code", None)
        if isinstance(code, int) and not isinstance(code, bool) and 300 <= code < 400:
            raise RedirectRefused(code) from None
        raise

def post(endpoint, body, timeout=15):
    c = creds()
    hdrs = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(str(random.getrandbits(32)).encode()).decode(),
            "iLink-App-Id": "bot", "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 9),
            "Authorization": "Bearer " + c["bot_token"].strip()}
    body = dict(body, base_info=BASE_INFO)
    url = (c.get("baseurl") or "https://ilinkai.weixin.qq.com").rstrip("/") + "/" + endpoint
    r = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), headers=hdrs, method="POST")
    with urlopen_no_redirect(r, timeout=timeout) as f:
        return f.read().decode()

def send_text(text, to=None, context_token=None):
    """Send one text. to defaults to the last user who wrote, else the binder (ilink_user_id).
    context_token=None -> use the latest stored token for that user; "" -> send without one."""
    st = load_state(); c = creds()
    to = to or st.get("last_from_user_id") or c["ilink_user_id"]
    if context_token is None:
        context_token = st.get("context_tokens", {}).get(to)
    msg = {"from_user_id": "", "to_user_id": to, "client_id": "openclaw-weixin-" + secrets.token_hex(8),
           "message_type": 2, "message_state": 2, "item_list": [{"type": 1, "text_item": {"text": text}}]}
    if context_token: msg["context_token"] = context_token
    resp = post("ilink/bot/sendmessage", {"msg": msg})
    # iLink answers HTTP 200 even on refusal; success = no non-zero ret / errcode in the JSON body.
    try:
        r = json.loads(resp) if resp.strip() else {}
        ok = not (isinstance(r, dict) and (r.get("ret") or r.get("errcode")))
    except ValueError:
        ok = False
    if ok:
        try:
            import history; history.append(history.grok_line(text))
        except Exception:
            pass
    return resp, bool(context_token)
