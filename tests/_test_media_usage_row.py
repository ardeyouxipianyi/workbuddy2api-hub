"""媒体调用的 usage 行必须带上计费参数，否则事后无法解释金额。

一行只有 `credit: 57.14` 是没法复核的：为什么这次 57.14、那次 128.57？模型、
秒数、分辨率任何一个没写下来，就只能靠翻任务表猜。而这个网关会同时服务多个
模型和多个分辨率档位。

No network: the log is written to a temp directory.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-media-usage-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_proxy as P

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, extra))


def rows():
    with open(P.USAGE_LOG, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def last():
    return rows()[-1]


def main():
    print("media usage rows carry billing parameters")

    P.record_usage("minimax-video-h3", {"credit": 57.14}, stream=False,
                   account="u-1", outcome="completed", key="k-1", realm="cn",
                   kind="video",
                   media={"seconds": 4, "resolution": "768P",
                          "aspect_ratio": "16:9", "image_to_video": False})
    row = last()
    check("kind is video", row.get("kind") == "video", row.get("kind"))
    check("credit recorded", row.get("credit") == 57.14, row.get("credit"))
    check("seconds recorded", row.get("seconds") == 4, row.get("seconds"))
    check("resolution recorded", row.get("resolution") == "768P", row.get("resolution"))
    check("aspect ratio recorded", row.get("aspect_ratio") == "16:9", row.get("aspect_ratio"))
    check("image_to_video recorded", row.get("image_to_video") is False,
          row.get("image_to_video"))
    check("cost policy is media", row.get("cost_policy") == "media", row.get("cost_policy"))

    # chat 行不该凭空多出这些字段——否则每一行都多几个 null
    P.record_usage("gpt-4o", {"prompt_tokens": 5, "completion_tokens": 2,
                              "total_tokens": 7}, stream=False, kind="chat")
    row = last()
    for field in ("seconds", "resolution", "aspect_ratio", "image_to_video"):
        check("chat row has no %s" % field, field not in row, row.get(field))

    # 显式 None 也不写：image_to_video=True 才写，None 说明没这个参数
    P.record_usage("kling-video-v3", {"credit": 128.57}, stream=False, kind="video",
                   media={"seconds": 5, "resolution": None, "aspect_ratio": None})
    row = last()
    check("None params omitted", "resolution" not in row and "aspect_ratio" not in row,
          {k: row.get(k) for k in ("resolution", "aspect_ratio")})
    check("seconds still present", row.get("seconds") == 5, row.get("seconds"))

    print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
