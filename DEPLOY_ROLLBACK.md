# 出站图片：部署与回滚

这次只在用户自己的 fork 上提交。不要推到 `Tencent/openclaw-weixin`，也不要在没经使用者再次确认时发真实图片或重启转发器。

数据外传结论见 `SECURITY_REVIEW.md`：这份源码里没有发现作者控制的接收端。生产目录的 `.venv` 和 `run_forward.sh` 不在那次阅读范围内，所以不能用这份结论代替对正在运行的那份拷贝的检查。上线前在机器上核对 `WECHAT_WEBHOOK_URL`、凭证里的 `baseurl` 和 `https_proxy`，不要把这些值贴出来。

生产机上的 `/workspace/grokbot-wechat` 有改过的 `.venv/` 和 `run_forward.sh`，这份仓库看不到那些改动。不要假设生产目录和这里的 `main` 一样。

## 不要覆盖

- `.venv/`
- `run_forward.sh`
- `watchdog.sh`（本次没有改它；若生产版不同，保留生产版）
- `.env`、`credentials.json`、`state.json`、`pending.json`、`outbox.json`、`history.log`、`forward.log`、`attachments/`

重定向修复改了 `ilink.py`、`forward.py`、`bind.py`（以及图片上传共用的拒绝跳转类）。`poll.py`、`outbox.py`、`send.py`、`media.py`、`watchdog.sh` 没有为这件事改行为。200 响应的文字发送逻辑不变。生产进程在有人把这些文件拷上去并自行重启之前，仍是旧代码。这次不重启。

## 需要放上的文件

新文件：

- `send_image.py`
- `image_send.py`
- `tests/test_outbound_image.py`
- `tests/test_redirect_guard.py`
- `SECURITY_REVIEW.md`（给人看的，转发器不读它）

重定向修复还要换上这些已有文件（不要用仓库版本覆盖生产上改过的 `run_forward.sh`）：

- `ilink.py`
- `forward.py`
- `bind.py`

有改动、需要合并而不是整文件覆盖的：

- `README.md`（出站图片用法、退出码、关闭方式）
- `.env.example`
- `.gitignore`（账本文件名）
- `routines/inbound-webhook.md`（agent 模板。生产 routine 若已改过措辞，只把图片那几条合并进去）

没有新的 Python 依赖。图片加密用的是原来的 `cryptography`。用生产环境真正跑 `send.py` 的那个解释器执行下面的检查，不要另装一套：

```bash
python3 -c "import cryptography, image_send"
python3 -m unittest discover -s tests -v
```

期望：全部通过，且过程中没有对外连接。

## 启用之前

默认 `WECHAT_OUTBOUND_IMAGE` 未设置时，图片命令是开的。在你确认要试发之前，把下面这行放到**运行 `send_image.py` 的环境**（不必重启 `forward.py`，这个变量每次调用都会读）：

```bash
WECHAT_OUTBOUND_IMAGE=0
```

试发时去掉该限制，并带上当时那一轮的 `--reply-to-ms`。收件人只能是 `credentials.json` 里的 `ilink_user_id`。

退出码：

| 码 | 含义 | 要不要再跑同一条命令 |
|---|---|---|
| 0 | 已发出 | 不要 |
| 1 | 校验或上传失败，微信消息还没发 | 原因修好后可以 |
| 2 | 功能被环境变量关掉 | 不要，除非有意打开 |
| 4 | 被更新的用户消息取代 | 不要，等新的一轮 |
| 5 | 结果不明、被拒绝，或账本认为已经试过 | 不要 |

CDN 若返回 `novac2c.cdn.weixin.qq.com` 以外的 https 主机，会失败关闭。只有在确认该主机属于腾讯 CDN 之后，才把精确主机名写入 `WECHAT_CDN_HOST_ALLOWLIST`。

## 补丁对不上时

生产树和这份 `main` 不一样时，`git apply` 可能在 README 上失败。那时只复制上面的新文件，再把 README 里「出站图片」一节手工补上。不要为了让补丁应用而检出干净的 `run_forward.sh`。

## 回滚

1. 设 `WECHAT_OUTBOUND_IMAGE=0`，或停止调用 `send_image.py`。文字 `send.py` 不受影响。
2. 不需要为此重启转发器或看门狗。
3. 若要删掉功能：去掉 `send_image.py` 和 `image_send.py`。账本 `image_send_ledger.json` 在数据目录，可留着（里面没有 AES key）。
4. 用 git 时，回退本功能的提交即可。不要回退生产机上与 `.venv` 或 `run_forward.sh` 有关的本地提交。

## 还不能当作已经打通

离线测试覆盖了填充、密钥的 base64(hex) 编码、上传字段、CDN 不带 Bearer、缺上传地址、HTTP/业务错误、上传前和发送前的取代判断、结果不明时不重试、图片失败不进文字 outbox、以及文字队列仍会在失败时入队。没有对真实微信或真实 CDN 发过请求。服务端是否要求缩略图字段、是否接受这份 `User-Agent`、空 JSON 是否一定表示成功，都还没有实机答案。
