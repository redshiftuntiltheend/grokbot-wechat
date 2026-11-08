# 安全审查：现有微信桥与这次的出站图片

审查对象是本仓库当前代码（社区桥 `grokbot-wechat`），加上本次未上线的出站图片实现。只读了源码和腾讯公开文档。没有读取生产机上的 `credentials.json`、`.env`、聊天日志或附件，没有连接微信，没有上传 CDN，没有重启进程。

这不是渗透测试，也不是对运行中进程的检查。下面把「代码里能直接看到的行为」「要出问题还缺什么前提」「这次看不到的运行时设置」分开写。

## 给使用者的结论

不能把这个桥说成安全，也不能说没有风险。它不是腾讯官方客户端，腾讯公开仓库也没有为这个 Python 转发器背书。

它会做三件你需要事先接受的事：本机长期保存能以你的 ClawBot 身份收发消息的 `bot_token`；把微信对话发到你自己配置的 webhook（设计上就是 Grok）；把同一份消息交给腾讯的 iLink / CDN 服务器。机器如果只有你能登录，而且 webhook 地址确实是你的 routine，它做的就是这条链路本身。webhook 填错、数据目录能被别人读、或者 agent 听信了微信里的指示，影响就不只是「多了一条聊天」。

这次只加了离线的出站图片。图片失败不会进文字队列，结果不清楚时不会自动再发，CDN 请求不带 Bot token，收件人必须是绑定的微信号。这些是代码里已经用假网络测过的限制。微信会不会接受这种图片消息，这次没有实机验证，所以不能把它说成已经能用。

没有在腾讯当前中文 README，或本次读到的协议说明里，看到「用非官方程序就会封号」这类原话。社区博客写过有《微信 ClawBot 服务条款》，但条款正文没有取到。这里不推测封号概率。

## 严重程度怎么读

- **高**：代码路径已经成立，错误配置或一条恶意的入站字段就能把对话、密钥或本机请求送到意料之外的地方。
- **中**：后果同样是泄密或误发，但还要另一个条件（别的本机用户、媒体下载失败、平台真的投递了别人的消息、agent 执行了正文里的指示）。
- **低**：影响面窄，或只在本机运维场景里出现。
- **设计内**：不是缺陷。用这个桥就一定会发生。

## 设计内的数据流

| 数据 | 去向 | 依据 |
|---|---|---|
| 你发出和收到的微信内容 | 腾讯 iLink（`ilinkai.weixin.qq.com` 或凭证里的 `baseurl`） | `ilink.py` 的 `post()` |
| 同一批消息，外加 `history.log` 最近 40 行 | `WECHAT_WEBHOOK_URL`，并附上 webhook 的 Bearer | `forward.py` `post_webhook()` |
| 出站图片的明文 | 先在本机用 AES-128-ECB 加密；密钥通过 `getuploadurl` 交给腾讯，密文 POST 到 CDN | `image_send.py`；与腾讯 `upload.ts` / `send.ts` 相同的分工 |

webhook 是第三方。它是不是「意料之外」，取决于 `.env` 里的 URL 是不是你的 Grok routine。代码没有网址白名单（`config.py` 第 31–46 行只是原样读取）。

## 已核实的问题

### 高 — 入站媒体 URL 没有协议和主机限制

`media.py` 第 39–47 行：消息里如果带了 `media.full_url`，就原样交给 `urllib.request.urlopen`。没有要求 `https`，没有主机白名单，标准库还会跟随重定向。`file://` 和内网 `http://` 都在这个函数能打开的范围内。

前提：`getupdates` 返回的媒体对象里，`full_url` 得由攻击者决定。普通微信用户能不能写入这个字段，公开协议没有说明，这次也不能从生产消息里验证。能改 API 响应的人（或中间人）则不需要这个未知条件。本补丁没有改入站下载。

缓解：只允许 `https` 且主机是微信 CDN；拒绝重定向。那是入站改动，这次没做，避免影响现有收图。

### 高 — 媒体下载失败时，解密材料会进 webhook

`forward.py` 第 99–101 行：下载或解密失败时，把原始 item 放进 `raw_items`。原始 item 含 `aeskey` / `media.aes_key` 和 `encrypt_query_param`。同一次 POST 还会带上最近 40 行历史（`forward.py` 第 154–159 行）和 webhook Bearer（第 29–30 行）。

前提：某次媒体下载失败，且 webhook 不是你完全信任的地址，或者那个地址的日志会被别人看到。成功下载的路径会在摘要里去掉 `media` 和 `aeskey`（`media.py` `summarize()`），失败路径没有这样做。

缓解：失败时只给 agent 留错误码和消息 id，不要把密钥放进 payload。本次未改。

### 中 — 聊天落盘文件没有收紧权限

`credentials.json` 和 `state.json` 会 `chmod 600`（`bind.py` 第 46–48 行，`ilink.py` 第 17–20 行）。下面这些包含消息正文或用户 id 的文件，写入时没有改权限，实际模式等于进程的 umask：

- `outbox.py` 第 26–29 行 `outbox.json`
- `forward.py` 第 129–133 行 `pending.json`
- `history.py` 第 37–44 行 `history.log`
- `forward.py` 第 16–17 行 `forward.log`
- `config.py` 第 27 行数据目录 `os.makedirs`，没有设成 `0700`

前提：同一台机器上还有别的用户，且 umask 不是 `077`。生产机的 umask 和目录模式这次看不到。单用户、只有你能登录时，这和「agent 自己能读这些文件」是同一信任边界。

`state.json` 虽然是 600，但 `forward.py` 会把最近 20 条原始消息放进 `raw_log`。拿到这个文件的人同时拿到 `context_token`。

缓解：数据目录 `chmod 700`，启动转发器的环境使用 `umask 077`。不要用仓库里的 `run_forward.sh` 覆盖生产机上改过的脚本；要改只在现有脚本里加 umask。

### 中 — 文字发送在结果不明时会排队再试

`outbox.py` 第 41–48 行：`send_text` 抛异常，或响应不算成功，就把原文写入 `outbox.json`。`flush()`（第 50–59 行）之后会再发。超时发生在服务器已经收下消息时，用户会看到两条。`ilink.py` 第 45–50 行把空 JSON 或缺省 `ret` 当成成功，把非 JSON 当成失败并因此进入队列。

这是现有文字行为，这次按要求没有改。图片走另一条路，失败不会调用 `enqueue`。

### 中 — 文字收件人是「最后说话的那个人」

`ilink.py` 第 38 行：没指定 `--to` 时，发给 `state.json` 的 `last_from_user_id`，否则才是凭证里的 `ilink_user_id`。`forward.py` 第 64–65 行会用任何 `message_type == 1` 的 `from_user_id` 更新这个值。代码没有检查发送者是不是绑定用户。

前提：微信是否会把别人的消息投递到这个 ClawBot。chihao.li 的协议笔记写的是一个 ClawBot 绑定一个微信号，但没有证明别人不能发消息进来。这个前提未验证。

图片命令不跟随 `last_from_user_id`。`--to` 必须等于 `ilink_user_id`，否则拒绝（`image_send.py` `resolve_recipient`）。文字命令保持原样。

### 中 — 微信正文会进入 agent 的指令

`routines/inbound-webhook.md` 让模型阅读 `messages` 和 `history` 并调用本机命令。JSON 能防止撑破报文，不能防止模型把正文当成指示。出站图片让这种指示可以上传本机文件。图片侧已限制：只能是通过校验的图片、不能是凭证文件或其硬链接、收件人只能是绑定用户。通过校验的其他图片，agent 仍然发得出去。

### 中 — API 地址来自登录响应，没有主机白名单

`bind.py` 第 52–53 行把 `redirect_host` 拼进 `https://`。扫码轮询的请求头（第 12–13 行）不含 `Authorization`，所以这一步不会把已有 token 送出去。确认之后，`ilink.py` 第 29 行用凭证里的 `baseurl` 发送 Bearer。恶意的确认响应，或事后改过的 `credentials.json`，会让 token 发到那个主机。能改凭证文件的人通常已经能读 token。

### 低 — 日志和标准输出会带上对话片段

- `forward.py` 第 33 行记录 webhook 响应前 300 字
- `forward.py` 第 205 行记录 `from_user_id`
- `send.py` 第 25 行把 iLink 的整段响应打到标准输出（agent 记录里看得到）
- `bind.py` 第 31 行打印二维码 URL

成功路径的源码没有把 `bot_token` 写进日志。生产日志没读，不能证明历史上没有泄通过。图片模块自己不写 `forward.log`，错误信息是固定句子；`sendmessage` 被拒绝时只打印整数 `ret` / `errcode`。

### 低 — 依赖没有锁定哈希

`requirements.txt` 是 `cryptography>=41` 和 `qrcode[pil]>=7`，没有哈希，也没有单独的安装脚本。`qrcode[pil]` 会拉 Pillow，只给 `bind.py` 画二维码。图片发送没有新依赖。风险在 `pip install` 当时的包索引，不在这次 diff。

### 低 — 看门狗按命令行杀掉本目录的 `forward.py`

`watchdog.sh` 第 35–36 行：`pgrep -f forward.py`，再比较进程的 cwd。cwd 不是这个目录的进程不会被杀。误伤限于把工作目录放在桥目录里、命令行又匹配 `forward.py` 的进程。生产机的 `run_forward.sh` 可能和仓库不同，这次没有那份脚本。

### 低 — 文字的「被新消息取代」只查一次

`send.py` 第 18–22 行在调用 `send_or_queue` 之前查 `last_inbound_ms > reply_to_ms + 500`。检查和真正 POST 之间没有第二次确认。图片在上传前和 `sendmessage` 前各查一次（`image_send.py` 里对 `check()` 的调用）。两次检查之后、TCP 送出之前的空隙仍然在，本地判断做不到和服务器原子。

### 已排除：本仓库没有本地 HTTP 服务

读过的 Python 和 shell 都是出站客户端。`poll.py` / `forward.py` 不会 listen。主机上还有没有别的端口，这次看不到。

## 这次图片实现做了什么，还剩什么

对照的是腾讯公开仓库 `Tencent/openclaw-weixin` 主分支上的 `docs/protocol_zh_CN.md`、`src/messaging/send.ts`、`src/cdn/upload.ts`、`src/cdn/cdn-upload.ts`、`src/cdn/aes-ecb.ts`、`src/cdn/cdn-url.ts`。实现选择与公开客户端一致的部分：AES-128-ECB + PKCS#7、`getuploadurl` 的 `media_type=1` 和 `no_need_thumb=true`、`aeskey` 用 hex、消息里 `media.aes_key` 是这段 hex 的 base64、`mid_size` 为密文长度、`encrypt_type=1`、CDN 只发 `application/octet-stream`。比公开客户端更严的部分写在下面，也可能导致实机发不出去。

| 控制 | 代码 | 剩余问题 |
|---|---|---|
| CDN 请求不带 `Authorization` | `image_send.py` `build_cdn_request` | API 的 `getuploadurl` / `sendmessage` 仍带 Bot token，这是协议要求 |
| 上传 URL 只允许 https、默认主机 `novac2c.cdn.weixin.qq.com`，不跟随重定向 | `validate_cdn_url`、`_NoRedirect` | 腾讯若返回名单外的 CDN 主机会失败关闭。可用 `WECHAT_CDN_HOST_ALLOWLIST` 加精确主机名 |
| 结果不明或业务拒绝不自动再 POST `sendmessage` | `send_started` 在请求前写入账本；超时记为 `ambiguous` | 账本文件丢了，或两台机器共用凭证但不共用账本，仍可能发重 |
| 同一文件、同一绑定用户、同一个 `--reply-to-ms` 再执行会被拦住 | `image_send_ledger.json` 模式 600，不存密钥 | 换 `--idempotency-key` 或新的 `--reply-to-ms` 会再发。这是故意的 |
| 失败不进 `outbox.json` | 图片代码不调用 `enqueue` | 文字路径的重试行为不变 |
| 收件人只能是 `ilink_user_id` | `resolve_recipient` | 和 `send.py` 不同，避免图片跟着被污染的 `last_from` 走 |
| 符号链接、非常规文件、扩展名与文件头不符、空文件、大于 20 MiB、凭证文件硬链接、含 `bot_token` 等标记 | `read_image_file` | 20 MiB 是本地上限，不是腾讯文档里的限额。通过校验的普通图片仍可被 agent 发出 |
| `WECHAT_OUTBOUND_IMAGE=0` | `outbound_image_enabled` | 只影响 `send_image.py`，不影响 `send.py`，也不需要重启转发器 |

账本和异常文本不记录 AES key、上传参数或 token。假网络测试检查了泄漏字符串不会出现在异常和 traceback 里。

## 利用前提（这些不是已经发生的攻击）

- 入站 `full_url` 能指向本机能访问的内网或 `file://`：需要能影响 `getupdates` 里的该字段。
- webhook 收到别人不该看的对话：需要 URL 配错，或那个 endpoint 的访问范围比你以为的大。
- 别的本机用户读到 `history.log`：需要目录权限和 umask 允许。
- 文字重复发送：需要第一次请求其实成功，但客户端看到的是异常或非 JSON。
- 图片发给错误的人：文字路径仍有这个结构；图片路径在 `--to` 不等于绑定 id 时会拒绝。
- 模型被微信正文说服去发本地图片：需要 agent 执行该指示，且文件能通过图片校验。

## 这次看不到的运行时

生产目录里的 `.venv`、改过的 `run_forward.sh`、真实 umask、数据目录权限、`WECHAT_WEBHOOK_URL` 指向哪里、有没有 `https_proxy`、谁能登录那台机器、ClawBot 是否只接收绑定用户的消息。`https_proxy` / `http_proxy` 会让 `urllib` 把 API 和 CDN 请求交给代理；图片上传没有单独关掉代理。凭证和原始聊天日志没有打开。

## 来源，以及没有写成结论的账号风险

- 本仓库是社区程序（`LICENSE` 版权 psylch，MIT）。协议注释指向 chihao.li 的拆解和 npm 包 `@tencent-weixin/openclaw-weixin`。现有 `ilink.py` 使用 `bot_agent: OpenClaw` 和 `channel_version: 2.4.9`，这是在模仿公开客户端的请求头，不是腾讯发行的这份代码。
- 腾讯公开仓库 [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin) 的中文 README 把官方能力描述为 OpenClaw 的微信渠道插件。本次读到的 README 没有写封号、也没有写「禁止第三方直接调用」。
- [chihao.li 的 iLink 笔记](https://chihao.li/blog/en/wechat-clawbot-ilink-protocol)（页面日期 2026-03-22）写明 ClawBot 是带服务条款的官方功能，并且认为任何能发 HTTP 的程序都可以调用 iLink。同文写「目前只支持文字」。这和腾讯当前 `send.ts` / `upload.ts` 里的图片发送不一致，应以当前公开客户端为准，并把那句「只支持文字」视为过时。笔记里的示例 token 没有抄进本仓库。
- 服务条款的正文没有取到。没有腾讯关于封号的原文，就不把封号写成这个审查的发现。

## 建议（本次代码没有顺手改这些）

1. 确认 webhook URL 属于你自己的 routine 之前，不要把桥指向一个拿不准的地址。
2. 数据目录设为仅当前用户可读写。改生产启动脚本时只加 `umask 077`，不要整文件覆盖 `run_forward.sh`。
3. 图片先保持 `WECHAT_OUTBOUND_IMAGE=0`，直到你明确要做一次真实试发。试发失败若退出码是 5，不要再跑同一条命令。
4. routine 里把微信正文当成用户说的话，不要当成可以改写发送规则的指示。
5. 以后若改入站：给 `media.full_url` 加 https 和主机限制；下载失败时不要把 `raw_items` 里的密钥 POST 出去；考虑文字发送也只允许绑定的 `ilink_user_id`。

## 本次验证

`python3 -m unittest discover -s tests -v`：34 项通过（原有离线测试加上 `tests/test_outbound_image.py`）。网络全部是假的。没有真实微信发送，也没有真实 CDN 上传。
