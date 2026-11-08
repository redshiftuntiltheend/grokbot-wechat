# 数据外传审查：上游作者或 fork 相关方能否拿走消息、图片、凭证

审查范围是本仓库这份源码（含这次的出站图片）。没有打开生产机上的凭证、`.env` 或聊天日志，没有联网发图，没有部署。这是静态阅读，**不能保证**没被审到的文件、已安装的依赖或生产目录里另有外传。

## 直接回答

**在这份源码里，没有发现把文字、图片或凭证发给上游作者或 fork 相关第三方的证据。** 没有写死的作者域名、没有遥测、没有远程拉代码、跟踪文件里也没有嵌入的真实 token 或 webhook。

把仓库 fork 到 GitHub，**不会**让上游因此读到你机器上正在跑的消息或 `credentials.json`。上游能看到的是公开的源代码（如果仓库是公开的）。运行时数据留在跑程序的那台机器上，除非程序自己把数据发出去。

能把数据送走的，是另外三件事，而不是「fork」这个关系本身：

1. 代码或依赖里被放进恶意的发送逻辑（这份源码里没看到；生产 `.venv` 没审到）。
2. 你自己的配置把目的地指到别处（`WECHAT_WEBHOOK_URL`、代理、CDN 白名单）。这次读不到生产环境里的值。
3. 登录响应、`baseurl`、入站 `full_url`，或 HTTP 重定向给出一个不是腾讯的地址。代码会跟随其中一部分。这不是作者写死的地址，也没有证据表明腾讯会把流量转到作者。

微信服务器和你配置的 Grok webhook 收到消息，是这条桥的用途，**不算**被作者偷走。下面把这两类分开。

## 域名分类

| 域名或目的地 | 分类 | 运行时会不会发用户数据 |
|---|---|---|
| `ilinkai.weixin.qq.com` | 预定接收方：微信 iLink。`bind.py` 第 10 行、`ilink.py` 第 29 行、`credentials.example.json` 的默认值 | 会。见下表。代码里的默认 API |
| `novac2c.cdn.weixin.qq.com` | 预定接收方：微信 CDN。`media.py` 第 5 行、`image_send.py` 第 23–24 行 | 会。入站下载密文；出站只上传图片密文，不带 Bot token |
| `WECHAT_WEBHOOK_URL` | 预定接收方，**仅当**它是你自己的 Grok routine。代码没有默认 URL，也没有网址白名单（`config.py` 第 31 行） | 会，发到你填的地址。未设置则不发（`forward.py` 第 22–25 行） |
| `psylch/grokbot-wechat`、`chihao.li`、`Tencent/openclaw-weixin` | 文档和注释里的链接。README 第 148 行是给人看的 clone 地址 | **不会。** 桥进程不请求这些域名 |
| `github.com/redshiftuntiltheend/grokbot-wechat` | 这个工作区的 git remote，只在 `git push` 时用到 | 桥进程不使用。push 出去的是源码，不是聊天记录 |
| 凭证里的 `baseurl` | 登录响应写进 `credentials.json` 之后，`ilink.py` 第 29 行原样使用 | 若该值和默认不同，API 和 Bot token 会发到那个主机。源码没把它写成作者的域名 |
| `redirect_host` | 扫码状态里的字段。`bind.py` 第 52–53 行拼成 `https://` + 主机 | 只会把二维码轮询发过去。这一步的请求头不含 `Authorization`（`bind.py` 第 12–13 行） |
| 入站 `media.full_url` | 消息里带来的 URL。`media.py` 第 39–47 行原样 `urlopen` | 会向该 URL 发 GET。不附带凭证文件。URL 本身来自消息 |
| `upload_full_url` | `getuploadurl` 的返回值。图片路径只接受允许列表里的 https 主机，并拒绝重定向 | 不在名单里就不会 POST 密文 |
| `WECHAT_CDN_BASE` / `WECHAT_CDN_HOST_ALLOWLIST` | 运行环境覆盖，默认仍是上面的微信 CDN | 只有运行者自己改了才会扩大 |
| `http_proxy` / `https_proxy` | 源码没写代理，但 `urllib` 默认会用环境变量 | 生产环境有没有代理，这次看不到。有的话，API 和 webhook 会经过该代理 |

没有在可执行代码里找到 `x.ai`、`grok.com`、作者个人域名或分析服务的请求。

## 每个出站调用带什么

预定发给微信 API（`ilink.py` `post()`，`bind.py` 的二维码接口）：

| 调用 | 数据 | 凭证 |
|---|---|---|
| `get_bot_qrcode` / `get_qrcode_status` | 二维码标识，可能还有你写入的验证码 | 不带 Bot token |
| `getupdates` | 同步游标 `get_updates_buf` | Bearer `bot_token` |
| `sendmessage`（文字） | 文本、`to_user_id`、`context_token`、`client_id` | 同上 |
| `getuploadurl`（图片） | 明文长度、MD5、密文长度、AES key 的 hex、收件人 | 同上 |
| `sendmessage`（图片） | CDN 下载参数、hex 密钥的 base64、密文长度 | 同上 |
| CDN 上传（图片） | 只有密文。`Content-Type: application/octet-stream` | **不带** Bot token（`image_send.py` `build_cdn_request`） |
| CDN 下载（入站） | GET。自建 URL 时带 `encrypt_query_param` | 不带 Bot token |

预定发给你配置的 webhook（`forward.py` `post_webhook`）：

- 这批微信消息的文本、类型、已解密文件的本地路径
- `history.log` 最近 40 行
- 媒体下载失败时的 `raw_items`（含 AES key 和 CDN 参数）——`forward.py` 第 99–101 行
- 请求头里是 **webhook** 的 Bearer，不是微信 `bot_token`
- 源码没有把 `credentials.json` 放进这个 JSON

本地文件（不自动上传）：`credentials.json`、`state.json`、`history.log`、`pending.json`、`outbox.json`、`forward.log`、附件目录、图片账本。`.gitignore` 忽略了凭证、`.env`、状态、历史和日志。跟踪文件里的 token 是占位符（`credentials.example.json` 第 4 行，`.env.example` 第 5–7 行）。git hooks 只有 Git 自带的 `*.sample`，没有启用的外传 hook。

## 配置从哪里来，有没有作者留下的开关

- Webhook URL 和 key：环境变量，或代码目录下的 `.env`。`.env` **不会**覆盖已经存在的环境变量（`config.py` `_load_dotenv`）。key 也可以来自 `WECHAT_SECRETS_FILE` 指向的本地 JSON。
- 没有内置的「更新检查」、没有 `exec` / `eval` / `importlib` 拉远程代码、没有安装脚本。`requirements.txt` 只有 `cryptography>=41` 和 `qrcode[pil]>=7`，没有哈希。
- `run_forward.sh` 和 `watchdog.sh` 只在本地启动或重启 `forward.py`，没有 `curl` 或 `wget`。
- 图片开关 `WECHAT_OUTBOUND_IMAGE` 只决定发不发图片，不增加目的地。

## 不是作者地址、但代码仍可能连到别的主机

这些不是「已经发现作者在收数据」，而是源码允许的条件路径：

- `ilink.post` 和 webhook、入站下载用的是 `urllib.request.urlopen`。Python 3.12 对 POST 的 301/302/303 会跟到 `Location`，并且**保留 `Authorization`**，但不把 POST 正文拷到下一次请求（标准库 `urllib/request.py` 的 `redirect_request`）。因此：若微信或 webhook 的第一跳返回重定向，Bot token 或 webhook key 可能被带到 `Location` 那个主机；消息正文仍只送到第一跳。图片 CDN 上传拒绝重定向。
- 入站 `full_url` 没有主机限制。它让桥去 GET 一个消息里给出的地址，不是把凭证文件传出去。
- 扫码阶段的 `redirect_host` 没有白名单，但那个请求不带已有的 Bot token。

## 依赖

本审查环境里的 `cryptography` 是系统包。它的 Python 源码里有文档用的 `https://` 链接，**没有** `urlopen` 或 `requests`。没审它的 OpenSSL 二进制。`qrcode` 和 Pillow 在这个环境里没装，所以没读它们的代码。生产 `.venv` 可能是另一套包，不在本次范围内。

## 上线前要拦住的事

- **不能**根据这次审查宣布生产那份拷贝没有外传。`/workspace/grokbot-wechat` 上改过的 `.venv` 和 `run_forward.sh` 没看到。这是宣称「正在跑的那份是安全的」之前的阻塞项。
- 图片功能仍然不要真实发送、不要部署，直到使用者另行确认。阻塞原因是没做过真实微信/CDN 试验，**不是**因为发现了作者后门。
- 启用或继续使用之前，在生产机器上核对（不要把值贴出来）：`WECHAT_WEBHOOK_URL` 是不是你自己的 Grok webhook；`credentials.json` 的 `baseurl` 是不是 `https://ilinkai.weixin.qq.com`；环境里有没有指向别人的 `https_proxy`。这三项目前都看不到。

没有发现「必须先删掉某段作者接收端才能上线」的代码。

## 这份审计没有覆盖、所以不能算排除

生产目录的实际文件、它的 `.venv`、进程环境、代理、webhook 的真实值、已安装 wheel 的内容、以及腾讯会不会在响应当中重定向。静态阅读看不到运行之后才出现的流量。
