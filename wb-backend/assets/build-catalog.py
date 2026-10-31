#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Python 版的内置模型目录导出成 Rust 侧的数据文件。

目录内容（ID 顺序、capabilities、limits、能力标注）全部由 Python 的
`merge_catalog` + `model_entry` 决定；Rust 侧只负责「created 取当前时间 +
按 realm/Key/账号过滤 + 打 entitlement 标」。这样目录只有一份真源，等 M4 把
活数据源（remote / 桌面缓存 / models.dev）搬过去时，这份文件自动退化成兜底。

    ACCOUNTS_DIR=$(mktemp -d) python3 wb-backend/assets/build-catalog.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("ACCOUNTS_DIR", "/tmp/wb-catalog-build")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_proxy  # noqa: E402

for realm in ("intl", "cn"):
    entries = wb_proxy.merge_catalog([], realm=realm, extras=False)
    data = [wb_proxy.model_entry(mid, meta) for mid, meta in entries]
    for item in data:
        item.pop("created", None)  # 每次请求现取时间，落盘就固定住了
    path = os.path.join(OUT, "catalog-%s.json" % realm)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print("%s: %d models -> %s" % (realm, len(data), path))
