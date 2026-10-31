# 回放夹具（验收用）

两个后端（Python / Rust）跑**同一批请求**、对**同一个假上游**，把响应逐字节比出来。
不碰外网、不扣积分。

```
python3 harness/compare.py --self-check                       # 夹具自检（CI 可跑）
python3 harness/fake_upstream.py --bootstrap /tmp/wb-fixture  # 造一份假账号+设置
python3 harness/fake_upstream.py --port 9444                  # 起假上游
python3 harness/compare.py --base http://127.0.0.1:8090 --base http://127.0.0.1:8091
```

## 它保证什么

- **同一后端两次抓取逐字节一致**：先证明差值不是噪声，再谈两个后端是否等价。
- **上游答案一变，比对必然会红**：`--self-check` 会故意改一份答案，确认比对器不是永远绿。
- **请求必须落在假上游**：落空就报错，不会静默打真上游（那既烧额度又让夹具失去意义）。

## 已知要点

- 假账号的 `product` 必须是 `workbuddy`（桌面身分）。`vscode` 身分的 cn 对话端点
  是硬编码的 `www.workbuddy.cn`，**不吃** `WB_CN_CHAT_UPSTREAM`——用 vscode 夹具会
  悄悄打到真上游。
- 网关对内**总是按流式**取上游（非流式客户端由它缓冲后再回），所以改假上游的答案要
  同时改 `completion_body` 与 `sse_frames`，只改前者时比对器不会红。
- 归一化去掉 `id` / `created` / `system_fingerprint` / `first_chunk_at` / `elapsed_ms` /
  `ttft_ms` / `gen_ms` / `iso` / `at`：这些每跑一次都不同，留着会让 diff 永远报错。
- 兜底答案按规则合成（不是随机）：没有夹具时回一条 11+2 token 的短答，保证结构稳定。
  某条用例的内容重要时，把 `fixtures/<sha>.json` 放进去即可（key =
  `sha256(model + "\n" + canonical(messages))[:16]`）。
