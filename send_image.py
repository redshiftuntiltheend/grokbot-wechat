#!/usr/bin/env python3
"""Send one local image to the bound WeChat user.

Usage:
  python3 send_image.py /path/to/image.jpg --reply-to-ms MS [--to USER_ID] [--no-context]
                        [--idempotency-key KEY]

This is a separate command from send.py. It does not send a caption and it never
writes outbox.json. --to must be the bound ilink_user_id (or omit it).

Exit codes: 0 sent, 1 validation or upload failed (nothing was sent), 2 disabled,
4 superseded, 5 do not retry (unknown outcome, business rejection, or duplicate).
"""
import argparse, sys
import image_send

def main(argv=None):
    p = argparse.ArgumentParser(description="Send one local image to the bound WeChat user")
    p.add_argument("image")
    p.add_argument("--to", help="must match credentials ilink_user_id; default is that user")
    p.add_argument("--no-context", action="store_true")
    p.add_argument("--reply-to-ms", type=int)
    p.add_argument("--idempotency-key", default="")
    a = p.parse_args(argv)
    try:
        result = image_send.send_image_file(
            a.image, to=a.to, context_token="" if a.no_context else None,
            reply_to_ms=a.reply_to_ms, idempotency_key=a.idempotency_key)
    except image_send.ImageSendError as e:
        if e.exit_code == 4:
            print("SUPERSEDED: a newer message arrived; image was not sent.")
        elif e.exit_code == 2:
            print("DISABLED: outbound image sending is off (WECHAT_OUTBOUND_IMAGE).")
        elif e.exit_code == 5:
            print("DO NOT RETRY: %s" % e)
        else:
            print("FAILED: %s" % e)
        return e.exit_code
    except Exception:
        print("FAILED: image was not sent")
        return 1
    print("sent client_id=%s" % result.get("client_id", ""))
    return 0

if __name__ == "__main__":
    sys.exit(main())
