#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体能力的端到端探针：验一次真实链路，并如实打印花了多少积分。

对应 `docs/MEDIA-TEST-PLAN.md` §6（L4）。这是**唯一会真花钱**的一层，所以：

  - 默认 `--dry-run`：只打印"将要做什么、预估多少积分"，一个请求都不发
  - 加 `--yes` 才真的执行
  - `--models` / `--video-reject` 是零成本项（只读目录、或故意用越界参数让上游在
    建任务之前退回）

做法：把 `accounts/` **复制**一份到临时目录，起一个隔离实例（独立端口、独立
usage/catalog/media 目录、`WB_MEDIA_ENABLED=1`），然后用真实 HTTP 调网关自己的接口。
用副本的原因：实例可能会刷新 token、写回凭证档，不该碰你的原件。

用法：

    python scripts/probe_media.py --models          # 只读目录（零成本）
    python scripts/probe_media.py --image --yes     # 出一张图（约 5.71 积分）
    python scripts/probe_media.py --video 4 --yes   # 出一条 4 秒视频（约 57 积分）
    python scripts/probe_media.py --video-reject    # 越界参数（零成本）
    python scripts/probe_media.py --all --yes       # 全套（约 63 积分起）

纯标准库，Python 3.9+。
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "sk-media-probe"

#: 单项预估成本（积分）。数字来自 2026-11-01 的真实验收，与 dsh 的实测一致。
COST_IMAGE = 5.71
COST_PER_SECOND = {"minimax-video-h3": 14.29, "kling-video-v3": 25.71}

IMAGE_PROMPT = "一只橘猫趴在窗台上晒太阳，水彩风格，暖色光线"
VIDEO_PROMPT = "一只橘猫在窗台上伸懒腰，阳光透过窗帘洒下光斑"


# ---------------------------------------------------------------- 基础设施

def free_port():
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class Instance(object):
    """一个隔离的网关实例（账号目录是副本）。"""

    def __init__(self, accounts_dir):
        self.tmp = tempfile.mkdtemp(prefix="wb-media-probe-")
        self.accounts = os.path.join(self.tmp, "accounts")
        shutil.copytree(accounts_dir, self.accounts)
        for name in ("usage", "catalog", "media", "logs"):
            os.makedirs(os.path.join(self.tmp, name), exist_ok=True)
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.log_path = os.path.join(self.tmp, "gateway.log")
        self.proc = None
        self._log_file = None

    def start(self, timeout=60):
        env = dict(os.environ)
        env.update({
            "ACCOUNTS_DIR": self.accounts,
            "WB_PROXY_USAGE_DIR": os.path.join(self.tmp, "usage"),
            "WB_PROXY_CATALOG_DIR": os.path.join(self.tmp, "catalog"),
            "WB_MEDIA_DIR": os.path.join(self.tmp, "media"),
            "WB_PROXY_LOGS_DIR": os.path.join(self.tmp, "logs"),
            "WB_MEDIA_ENABLED": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        self._log_file = open(self.log_path, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "wb_proxy.py"),
             "--port", str(self.port), "--api-key", KEY],
            cwd=ROOT, env=env, stdout=self._log_file, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.base + "/health", timeout=1) as response:
                    if response.status == 200:
                        return True
            except Exception:
                time.sleep(0.3)
        return False

    def stop(self):
        try:
            if self.proc is not None:
                self.proc.terminate()
                self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            if self._log_file is not None:
                self._log_file.close()
        except Exception:
            pass

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- HTTP
    def call(self, method, path, body=None, timeout=120):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", "Bearer " + KEY)
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"{}"), time.time() - started
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw or b"{}")
            except ValueError:
                parsed = {"raw": raw[:400].decode("utf-8", "replace")}
            return exc.code, parsed, time.time() - started

    def download(self, path, timeout=180):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as response:
            return response.read()

    def media_log(self, limit=15):
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as handle:
                lines = [line.rstrip() for line in handle if "media" in line.lower()]
        except Exception:
            return []
        return lines[-limit:]


# ---------------------------------------------------------------- 各项探针

def probe_models(inst):
    status, body, elapsed = inst.call("GET", "/v1/media/models")
    print("GET /v1/media/models → HTTP %d · %.1f 秒" % (status, elapsed))
    if status != 200:
        print("  ", json.dumps(body, ensure_ascii=False)[:400])
        return False
    models = body.get("models") or []
    print("  realm=%s enabled=%s，共 %d 个媒体模型：" % (body.get("realm"),
                                                    body.get("enabled"), len(models)))
    for model in models:
        generation = model.get("videoGeneration") or {}
        extra = ""
        if generation:
            extra = " · %s–%s 秒 · %s" % (
                generation.get("minSeconds"), generation.get("maxSeconds"),
                "/".join(generation.get("resolutions") or []))
        print("    %-26s %s%s" % (model["id"], ",".join(model.get("tags") or []), extra))
    return bool(models)


def probe_image(inst):
    print("POST /v1/images/generations  1024x1024（约 %.2f 积分）" % COST_IMAGE)
    status, body, elapsed = inst.call("POST", "/v1/images/generations",
                                      {"prompt": IMAGE_PROMPT, "size": "1024x1024"})
    print("  → HTTP %d · %.1f 秒" % (status, elapsed))
    if status != 200:
        print("  ", json.dumps(body, ensure_ascii=False)[:500])
        return None
    item = (body.get("data") or [{}])[0]
    credit = body.get("credit")
    print("  模型 %s · 实扣 %s 积分" % (body.get("model"), credit))
    print("  hint: %s" % body.get("hint"))
    out_path = None
    url = item.get("url")
    if url:
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                raw = response.read()
            magic = raw[:8].hex()
            print("  产物 %d 字节 · 魔数 %s · %s"
                  % (len(raw), magic,
                     "PNG" if raw.startswith(b"\x89PNG") else
                     "JPEG" if raw.startswith(b"\xff\xd8\xff") else "未知"))
            out_dir = os.path.join(ROOT, "media", "probe")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, "probe-image-%d.png"
                                    % int(time.time()))
            with open(out_path, "wb") as handle:
                handle.write(raw)
            print("  已保存:", os.path.relpath(out_path, ROOT))
        except Exception as exc:
            print("  下载失败（URL 约 24 小时有效，可自行取）: %s" % exc)
    elif item.get("b64_json"):
        print("  上游返回内联 base64（%d 字符）" % len(item["b64_json"]))
    return credit


def probe_video(inst, model, seconds):
    per_second = COST_PER_SECOND.get(model, 26.0)
    estimate = per_second * seconds
    print("POST /v1/videos  %s  %d 秒（预估 %.2f 积分）" % (model, seconds, estimate))
    status, body, elapsed = inst.call("POST", "/v1/videos",
                                      {"prompt": VIDEO_PROMPT, "model": model,
                                       "seconds": seconds})
    print("  → HTTP %d · 提交耗时 %.1f 秒" % (status, elapsed))
    if status not in (200, 202):
        print("  ", json.dumps(body, ensure_ascii=False)[:500])
        return None
    task_id = body.get("id")
    print("  任务 %s · %s · %s 秒 · %s"
          % (task_id, body.get("status"), body.get("seconds"), body.get("resolution")))

    started = time.time()
    last = None
    final = None
    while time.time() - started < 600:
        status, body, _ = inst.call("GET", "/v1/videos/%s" % task_id)
        if status != 200:
            print("  查询失败:", json.dumps(body, ensure_ascii=False)[:300])
            return None
        state = body.get("status")
        if state != last:
            print("  +%5.1f 秒  %s" % (time.time() - started, state))
            last = state
        if state in ("completed", "failed"):
            final = body
            break
        time.sleep(5)
    if not final:
        print("  !! 600 秒内没到终态（任务可能还在跑，可用 GET /v1/videos/%s 再查）" % task_id)
        return None
    print("  终态 %s · 轮询 %.1f 秒" % (final.get("status"), time.time() - started))
    if final.get("error"):
        print("  错误:", json.dumps(final["error"], ensure_ascii=False))
    url = final.get("url")
    if url:
        try:
            raw = inst.download(url)
            print("  产物 %d 字节 · 魔数 %s · %s"
                  % (len(raw), raw[:12].hex(),
                     "mp4" if b"ftyp" in raw[:12] else "未知"))
            out_dir = os.path.join(ROOT, "media", "probe")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, "%s.mp4" % task_id)
            with open(out_path, "wb") as handle:
                handle.write(raw)
            print("  已保存:", os.path.relpath(out_path, ROOT))
        except Exception as exc:
            print("  下载失败: %s" % exc)
    return final.get("credit")


def probe_video_reject(inst):
    """零成本：用一个上游必然在**建任务之前**退回的参数。"""
    print("POST /v1/videos  故意越界（seconds=1）——上游应在建任务前退回，不扣费")
    status, body, _ = inst.call("POST", "/v1/videos",
                                {"prompt": "夏夜萤火虫", "model": "kling-video-v3",
                                 "seconds": 1})
    print("  → HTTP %d" % status)
    print("  ", json.dumps(body, ensure_ascii=False)[:400])
    if status == 400:
        text = json.dumps(body, ensure_ascii=False)
        if "未扣积分" in text:
            print("  ✓ 明确说明未扣积分（客户端可立刻重试）")
        if "3" in text:
            print("  ✓ 本地就拦下了（目录 minSeconds=3），没有打到上游")
    return None


# ---------------------------------------------------------------- 入口

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="媒体能力的端到端探针（默认 dry-run，加 --yes 才花钱）")
    parser.add_argument("--models", action="store_true", help="只读媒体目录（零成本）")
    parser.add_argument("--image", action="store_true", help="文生图一张")
    parser.add_argument("--video", type=int, metavar="SECONDS", default=None,
                        help="文生视频（秒数，如 4）")
    parser.add_argument("--video-model", default="minimax-video-h3",
                        choices=sorted(COST_PER_SECOND), help="视频模型")
    parser.add_argument("--video-reject", action="store_true",
                        help="用越界参数验 REJECTED 链路（零成本）")
    parser.add_argument("--all", action="store_true", help="目录 + 图 + 视频")
    parser.add_argument("--yes", action="store_true", help="确认执行并承担积分消耗")
    parser.add_argument("--accounts", default=os.path.join(ROOT, "accounts"),
                        help="账号目录（默认仓库下的 accounts/，会被复制使用）")
    args = parser.parse_args(argv)

    if args.all:
        args.models = args.image = args.video_reject = True
        if args.video is None:
            args.video = 4

    todo = []
    estimate = 0.0
    if args.models:
        todo.append(("读媒体目录", 0.0))
    if args.image:
        todo.append(("文生图 1024x1024", COST_IMAGE))
    if args.video:
        todo.append(("文生视频 %s %d 秒" % (args.video_model, args.video),
                     COST_PER_SECOND.get(args.video_model, 26.0) * args.video))
    if args.video_reject:
        todo.append(("越界参数（预期上游拒绝）", 0.0))
    if not todo:
        parser.print_help()
        return 0
    estimate = sum(cost for _, cost in todo)

    print("将执行：")
    for name, cost in todo:
        print("  · %s%s" % (name, "" if cost == 0 else "  ≈ %.2f 积分" % cost))
    print("预估总消耗：%.2f 积分" % estimate)
    if estimate > 0 and not args.yes:
        print("\n这是 dry-run。确认要花这些积分请加 --yes。")
        return 0
    if not os.path.isdir(args.accounts):
        print("账号目录不存在：%s" % args.accounts)
        return 2

    inst = Instance(args.accounts)
    spent = []
    try:
        if not inst.start():
            print("实例没能在 60 秒内起来（端口 %d）" % inst.port)
            return 3
        print("\nisolated instance: %s\n" % inst.base)
        if args.models:
            probe_models(inst)
        if args.video_reject:
            probe_video_reject(inst)
        if args.image:
            credit = probe_image(inst)
            if credit is None:
                print("\n生图失败，后面的花钱项不再执行。")
                return 4
            spent.append(("文生图", credit))
        if args.video:
            credit = probe_video(inst, args.video_model, args.video)
            spent.append(("文生视频", credit))
        print()
        print("=" * 60)
        total = sum(c for _, c in spent if c)
        for name, credit in spent:
            print("  %-8s %s" % (name, "未拿到 credit" if credit is None else "%.2f 积分" % credit))
        print("  合计     %.2f 积分" % total)
        print("=" * 60)
        logs = inst.media_log()
        if logs:
            print("\n[实例日志 · media]")
            for line in logs:
                print("  " + line)
        return 0
    finally:
        inst.cleanup()


if __name__ == "__main__":
    sys.exit(main())
