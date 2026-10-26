"""recent 的行偏移索引：页内容、读取量、落盘与降级。

recent_usage 的每一页都要从日志尾部往回数到第 N×limit 条记录才定位得到：
第 1 页至少读回一个 256KB 块，realm 过滤命中率低时要读回整个文件，页深越大
代价越大。这一套钉住新加的「行偏移索引」——每 64KB 记一条「行边界 + 累计
记录数」，页直接从索引定位：

  * 页内容：索引可用时与老实现逐字节一致。对拍口径就是同一个进程里把索引
    关掉（WB_RECENT_INDEX=0 走 _recent_backward_page，老实现原样搬过去的
    那条路），多页号、realm 过滤、夹页、空页、坏行/空行/老行全部覆盖；
  * 读取量：页的读取是「一段（64KB）+ 页本身」的量级，不再按页深读回整份
    日志（计数桩：包装模块里的 open 数读了多少字节；老路的 256KB 底价也
    一并量出来，证明桩是活的）；
  * 落盘：往返（重启后采纳）逐字节一致、追加后只折增量、索引文件 <10KB
    （4 万行日志）；
  * 降级：坏 JSON / schema 不符 / 段宽不符 / key-tail 不符 / 日志被截断、被
    替换、同尺寸重写、被删 / 半行 / 写盘路径不可用——一律退回老路，结果正确
    且不抛错（宁多扫一次，不给错页）。降级用例都往索引里塞一个一眼可见的
    毒标记（总数 +1000）：校验放行它 → 结果里就会多出那个标记 → 用例当场
    失败，比只看「有没有加载」更直接；
  * 归属门槛：日志里有「没带 realm 字段、要查活账号池的老行」时，活池与磁盘
    归属不一致就不许用索引的 realm 数字（老路永远用活池，永远是当前该有的
    数字）。

No network: the usage log, the pricing snapshot and the settings file are
synthesised in a temp directory.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="wb-recentidx-")
USAGE_DIR = os.path.join(_TMP, "usage")
ACCOUNTS_DIR = os.path.join(_TMP, "accounts")
os.makedirs(USAGE_DIR, exist_ok=True)
os.makedirs(ACCOUNTS_DIR, exist_ok=True)
os.environ["WB_PROXY_USAGE_DIR"] = USAGE_DIR
os.environ["ACCOUNTS_DIR"] = ACCOUNTS_DIR
os.environ["WB_USAGE_CACHE"] = "0"          # checkpoint 与本套件无关
os.environ["WB_COUNT_TTL"] = "0"            # 老路的页数标签每次现算

import wb_proxy as P          # noqa: E402

import wb_usage as U  # noqa: E402
import wb_pricing             # noqa: E402

P.ACCOUNTS_DIR = ACCOUNTS_DIR
wb_pricing.set_data_dir(USAGE_DIR)
wb_pricing.set_settings_dir(ACCOUNTS_DIR)

USAGE_LOG = P.USAGE_LOG
INDEX = P.RECENT_INDEX_FILE

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


# --------------------------------------------------------------------------
# 读字节计数桩：包装模块里的 open，量「这次调用从磁盘读了多少字节」
# --------------------------------------------------------------------------
_REAL_OPEN = open
STATS = {"bytes": 0}


class _CountingFile(object):
    def __init__(self, fh):
        self._fh = fh

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def read(self, *a):
        data = self._fh.read(*a)
        STATS["bytes"] += len(data)
        return data

    def readline(self, *a):
        data = self._fh.readline(*a)
        STATS["bytes"] += len(data)
        return data

    def __enter__(self):
        self._fh.__enter__()
        return self

    def __exit__(self, *a):
        return self._fh.__exit__(*a)


def _counting_open(*a, **kw):
    return _CountingFile(_REAL_OPEN(*a, **kw))


U.open = _counting_open


def read_bytes(fn):
    """跑 fn()，返回 (结果, 读了多少字节)。"""
    STATS["bytes"] = 0
    out = fn()
    return out, STATS["bytes"]


# --------------------------------------------------------------------------
# 哪条路在跑：两个入口都包一层计数，断言「这次走的是索引 / 老路」
# --------------------------------------------------------------------------
CALLS = {"index": 0, "backward": 0}
_real_index_page = U._recent_index_page
_real_backward_page = U._recent_backward_page


def _spy_index_page(snap, realm, page, limit):
    CALLS["index"] += 1
    return _real_index_page(snap, realm, page, limit)


def _spy_backward_page(realm, page, limit):
    CALLS["backward"] += 1
    return _real_backward_page(realm, page, limit)


U._recent_index_page = _spy_index_page
U._recent_backward_page = _spy_backward_page


def reset_index():
    """清掉索引状态（等价于一次进程重启，只是磁盘上那份还在）。"""
    U._recent_index_state.update({
        "stamp": None, "usable": False, "giveup": False, "giveup_key": None,
        "key": None, "size": 0, "tail": b"", "realm_map": None,
        "legacy": False, "realms": [], "counts": {}, "row_counts": {},
        "lines": 0, "rows": 0, "segs": [], "wrote_size": 0, "wrote_at": 0.0})


def reset_count():
    U._count_cache.clear()
    U._count_state.update({"offset": 0, "key": None, "tail": b"", "n": 0,
                           "n_realm": {}})


def call_page(realm=None, limit=100, page_no=1, use_index=True, fresh=False):
    """(页文本, 这次走了哪条路)。文本不排序键：响应的 JSON 字节里键序可见。"""
    os.environ["WB_RECENT_INDEX"] = "1" if use_index else "0"
    if fresh:
        reset_index()
    reset_count()
    CALLS["index"] = 0
    CALLS["backward"] = 0
    text = json.dumps(U.recent_usage(limit=limit, realm=realm, page=page_no),
                      ensure_ascii=False)
    return text, dict(CALLS)


def page(**kw):
    return call_page(**kw)[0]


def first_diff(a, b, span=80):
    if a == b:
        return ""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return "@%d: %r vs %r" % (i, a[i:i + span], b[i:i + span])
    return "长度 %d vs %d" % (len(a), len(b))


def same_page(label, **kw):
    """索引路径 vs 老路：逐字节一致，且两条路确实都跑过。"""
    got, used = call_page(use_index=True, **kw)
    want = page(use_index=False, **kw)
    check(label + "：逐字节一致", got == want, first_diff(got, want))
    check(label + "：索引路径真的跑了", used["index"] > 0, used)
    return got


# --------------------------------------------------------------------------
# 数据：一行带全字段，外加坏 JSON / 空行 / 老行（没有 realm 字段）
# --------------------------------------------------------------------------
FROZEN = 1791445200.0


def jline(obj):
    return json.dumps(obj, ensure_ascii=False) + "\n"


def row(i, realm="intl", account="u1"):
    r = {"at": FROZEN - 100000 + i * 1.5, "model": "m-a", "stream": True,
         "outcome": "completed", "elapsed_ms": 1000, "ttft_ms": 300,
         "gen_ms": 700, "prompt_tokens": 10, "completion_tokens": 10,
         "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 20,
         "credit": 0.01, "account": account, "key": "k1",
         "tokens_per_sec": 90.0}
    if realm is not None:
        r["realm"] = realm
    return r


def write_log(lines):
    with io.open(USAGE_LOG, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(lines))


def append_log(lines):
    with io.open(USAGE_LOG, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(lines))


# 一张小日志：两个 realm、坏 JSON、空行、老行（有 account 没 realm）。
LINES = []
for i in range(60):
    LINES.append(jline(row(i, realm="cn" if i % 7 == 3 else "intl",
                           account="u%d" % (i % 3))))
    if i % 13 == 5:
        LINES.append("not json {oops\n")          # 坏 JSON：算总数、不算记录
    if i % 17 == 7:
        LINES.append("\n")                        # 空行：两边都不算
    if i % 19 == 11:
        LINES.append(jline(row(i, realm=None, account="u9")))   # 老行

PRICING = {"meta": {"usd_cny": 7.1},
           "models": {"m-a": {"unit": 1000000, "currency": "CNY",
                              "flat": {"input_cache_hit": 0.5,
                                       "input_cache_miss": 1.0,
                                       "output": 2.0}}}}
PRICING_FILE = os.path.join(_TMP, "pricing.json")
os.environ["WB_PRICING_FILE"] = PRICING_FILE
_MTIME = [FROZEN - 1000000.0]


def write_pricing(doc):
    io.open(PRICING_FILE, "w", encoding="utf-8").write(
        json.dumps(doc, ensure_ascii=False))
    _MTIME[0] += 3600.0
    os.utime(PRICING_FILE, (_MTIME[0], _MTIME[0]))


write_pricing(PRICING)
write_log(LINES)

# --------------------------------------------------------------------------
print("[1] 页内容：索引路径 vs 老路（多页号、realm 过滤、夹页、空页）")
# --------------------------------------------------------------------------
os.environ.pop("WB_RECENT_INDEX", None)
if os.path.exists(INDEX):
    os.unlink(INDEX)
reset_index()
reset_count()

for label, kw in (
        ("第 1 页（不滤 realm）", dict()),
        ("realm=all（与 None 同义）", dict(realm="all")),
        ("realm=intl 第 1 页", dict(realm="intl")),
        ("realm=intl 第 2 页", dict(realm="intl", page_no=2)),
        ("realm=cn 第 1 页", dict(realm="cn")),
        ("realm=cn 第 2 页", dict(realm="cn", page_no=2)),
        ("realm=cn 越界页（夹到最后一页）", dict(realm="cn", page_no=9)),
        ("不存在的 realm（空页）", dict(realm="zzz")),
        ("limit=1", dict(limit=1)),
        ("limit=1000（页比日志大）", dict(limit=1000)),
        ("页号远超总页数", dict(page_no=99)),
        ("limit=7 第 3 页", dict(limit=7, page_no=3))):
    same_page(label, **kw)

check("索引文件已经写出来了", os.path.exists(INDEX), os.listdir(USAGE_DIR))
check("索引文件不大（小日志：几 KB 以内）",
      os.path.getsize(INDEX) < 10 * 1024, os.path.getsize(INDEX))

# --------------------------------------------------------------------------
print()
print("[2] 读取量：页只读「一段 + 页本身」，不再按页深读回整份日志")
# --------------------------------------------------------------------------
page_text = page(use_index=True)
_, idx_bytes = read_bytes(lambda: page(use_index=True))
_, old_bytes = read_bytes(lambda: page(use_index=False))
check("索引路径第 1 页读得比老路少",
      idx_bytes < old_bytes, (idx_bytes, old_bytes))
check("老路第 1 页的底价是「一个 256KB 块或整份日志」（桩是活的）",
      old_bytes >= min(256 * 1024, os.path.getsize(USAGE_LOG)), old_bytes)
check("索引路径第 1 页 ≤ 一段 + 页大小",
      idx_bytes <= 64 * 1024 + len(page_text), (idx_bytes, len(page_text)))

deep = len(LINES) // 2
_, deep_idx = read_bytes(lambda: page(limit=1, page_no=deep, use_index=True))
_, deep_old = read_bytes(lambda: page(limit=1, page_no=deep, use_index=False))
check("深页（第 %d 页）索引路径仍 ≤ 一段 + 页大小" % deep,
      deep_idx <= 64 * 1024 + 4096, deep_idx)
check("深页老路读的是整份日志（桩是活的）", deep_old > deep_idx, (deep_old, deep_idx))

# --------------------------------------------------------------------------
print()
print("[3] 落盘往返：重启（新进程）采纳索引后逐字节一致，且只折增量")
# --------------------------------------------------------------------------
page(use_index=True)                       # 确保索引已落盘
stored = json.loads(io.open(INDEX, encoding="utf-8").read())
check("索引是一个 schema 版本正确的 JSON 对象",
      isinstance(stored, dict) and stored.get("schema") == 1
      and stored.get("seg") == 65536, sorted(stored.keys())
      if isinstance(stored, dict) else type(stored))
check("落盘的键就是位置/归属/计数那几样",
      sorted(stored.keys()) == ["key", "legacy", "realm_map", "realms",
                                "schema", "seg", "segs", "tail", "totals"],
      sorted(stored.keys()))
check("写盘是原子替换：没有留下临时文件",
      not os.path.exists(INDEX + ".tmp"), os.listdir(USAGE_DIR))

CASES = [("none", {}), ("cn", {"realm": "cn"}),
         ("intl2", {"realm": "intl", "page": 2}),
         ("one", {"limit": 1}), ("zzz", {"realm": "zzz"})]


def page_kw(kw):
    """子进程用 recent_usage 的原参数名（page），本文件的 page() 用 page_no。"""
    out = dict(kw)
    if "page" in out:
        out["page_no"] = out.pop("page")
    return out


GOLDEN = {}
for _key, _kw in CASES:
    GOLDEN[_key] = page(use_index=False, **page_kw(_kw))

RUNNER = r'''
import json, os, sys
sys.path.insert(0, __ROOT__)
import wb_proxy as P
import wb_usage as U
out = {}
for key, kw in __CASES__:
    out[key] = json.dumps(U.recent_usage(**kw), ensure_ascii=False)
with open(os.environ["WB_CHILD_OUT"], "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False)
print("child ok")
'''
CHILD = os.path.join(_TMP, "child_runner.py")
io.open(CHILD, "w", encoding="utf-8", newline="\n").write(
    RUNNER.replace("__ROOT__", repr(ROOT)).replace("__CASES__", repr(CASES)))


def run_child(extra_env=None):
    dest = os.path.join(_TMP, "child_out.json")
    if os.path.exists(dest):
        os.unlink(dest)
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["WB_CHILD_OUT"] = dest
    env.pop("WB_RECENT_INDEX", None)
    env.update(extra_env or {})
    done = subprocess.run([sys.executable, CHILD], cwd=ROOT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=300)
    text = done.stdout.decode("utf-8", "replace")
    if done.returncode != 0 or not os.path.exists(dest):
        raise AssertionError("子进程失败 rc=%s\n%s" % (done.returncode, text[-2000:]))
    with io.open(dest, encoding="utf-8") as fh:
        return json.load(fh)


child = run_child()
for key, kw in CASES:
    check("新进程采纳索引后 %s 一致" % key,
          child[key] == GOLDEN[key], first_diff(child[key], GOLDEN[key]))
child_off = run_child({"WB_RECENT_INDEX": "0"})
check("新进程 + 关掉索引：老路同样一致",
      all(child_off[k] == GOLDEN[k] for k, kw in CASES),
      [k for k, kw in CASES if child_off[k] != GOLDEN[k]])

# 追加：索引只折增量（读的字节数远小于整份日志），结果与老路一致
append_log([jline(row(100 + i, realm="cn" if i % 2 else "intl",
                      account="u%d" % (i % 3))) for i in range(20)])
for kw in ({}, dict(realm="cn"), dict(realm="intl", page_no=2)):
    got, used = call_page(use_index=True, **kw)
    want = page(use_index=False, **kw)
    check("追加后 %s 与老路一致" % (kw or "第 1 页"), got == want,
          first_diff(want, got))
    check("追加后 %s 走了索引" % (kw or "第 1 页"), used["index"] > 0, used)
check("追加后的结果确实变了（否则上面几条测不出东西）",
      page(use_index=True) != GOLDEN["none"], "追加前后第 1 页相同")

# --------------------------------------------------------------------------
print()
print("[4] 降级：索引坏了 / 日志变了，一律退回老路且结果正确（毒标记可见）")
# --------------------------------------------------------------------------
POISON = 1000


def poison(data):
    """往索引里塞一个一眼可见的错值：被采纳 → 总数多 1000 → 用例当场失败。

    只加在数字字段上（变异用例可能先把字段改成字符串，那是另一条要挡的路）。
    """
    def bump(table, key):
        value = table.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            table[key] = value + POISON

    totals = data["totals"]
    bump(totals, "lines")
    for name in list(totals.get("counts") or {}):
        bump(totals["counts"], name)
    for name in list(totals.get("row_counts") or {}):
        bump(totals["row_counts"], name)


def read_index():
    return json.loads(io.open(INDEX, encoding="utf-8").read())


def save_index(data):
    io.open(INDEX, "w", encoding="utf-8", newline="\n").write(
        json.dumps(data, ensure_ascii=False))


def mutate_index(mutate, label, poison_it=True):
    """造一份好索引 → 变异 + 塞毒 → 重启后取页：结果必须回到老路那份。

    poison_it=False 给「变异的就是毒标记要碰的那个字段」的用例用（否则变异会
    被毒标记改回一个合法值，等于没测）。
    """
    page(use_index=True)                       # 写一份好索引
    data = read_index()
    mutate(data)
    if poison_it:
        poison(data)
    save_index(data)
    got = page(use_index=True, fresh=True)     # 模拟重启：只能靠磁盘那份
    want = page(use_index=False)
    check(label + "：结果与老路一致", got == want, first_diff(want, got))
    check(label + "：毒标记没有漏出来",
          json.loads(got)["total"] == json.loads(want)["total"],
          (json.loads(got)["total"], json.loads(want)["total"]))


# 先证明毒标记本身是可见的：一份「结构完好但被塞毒」的索引会被采纳
page(use_index=True)
data = read_index()
poison(data)
save_index(data)
poisoned = page(use_index=True, fresh=True)
plain = json.loads(page(use_index=False))["total"]
check("标记机制自检：结构完好的索引被采纳时毒标记会出现在结果里",
      json.loads(poisoned)["total"] == plain + POISON,
      (json.loads(poisoned)["total"], plain))

mutate_index(lambda d: d.update({"schema": 2}), "schema 版本不符（比当前新）")
mutate_index(lambda d: d.update({"schema": "1"}), "schema 写成字符串")
mutate_index(lambda d: d.pop("schema"), "schema 缺失")
mutate_index(lambda d: d.update({"seg": 4096}), "段宽与当前实现不符")
mutate_index(lambda d: d.update({"tail": "zz"}), "tail 不是合法十六进制")
mutate_index(lambda d: d.update({"tail": 12345}), "tail 不是字符串")
mutate_index(lambda d: d.update({"key": [1, 2]}), "key 长度不对")
mutate_index(lambda d: d["key"].__setitem__(2, 987654321), "key 里的 inode 对不上")
mutate_index(lambda d: d["key"].__setitem__(1, 987654321), "key 里的 dev 对不上")
mutate_index(lambda d: d["key"].__setitem__(0, 10 ** 9), "key 里的 size 超出文件")
mutate_index(lambda d: d["totals"].update({"lines": "很多"}), "总数不是数字",
             poison_it=False)
mutate_index(lambda d: d["totals"].update({"lines": -1}), "总数是负数",
             poison_it=False)
mutate_index(lambda d: d["totals"].update({"counts": []}), "counts 不是对象")
mutate_index(lambda d: d["totals"].update({"counts": {"cn": "x"}}),
             "counts 里有非数字")
mutate_index(lambda d: d["totals"].update({"row_counts": None}), "row_counts 缺失")
mutate_index(lambda d: d.update({"realms": "cn"}), "realms 不是数组")
mutate_index(lambda d: d.update({"realms": ["cn", "cn"]}), "realms 有重复项")
mutate_index(lambda d: d.update({"realms": [5]}), "realms 里有非字符串")
mutate_index(lambda d: d.update({"segs": [[1, 2]]}), "段的行宽不对")
mutate_index(lambda d: d.update({"segs": [[65536, 1, 1, "x"]]}), "段里有非数字")
mutate_index(lambda d: d.update({"segs": [[65536, -1, 0, 0]]}), "段里有负数")
mutate_index(lambda d: d.update({"realm_map": [[1, 2]]}), "realm_map 形状不对")
mutate_index(lambda d: d.update({"legacy": "yes"}), "legacy 不是布尔")
mutate_index(lambda d: d.update({"legacy": 1}), "legacy 写成 1")

# 文件本身读不出来
for label, raw in (("损坏 JSON", b"{this is not json"),
                   ("顶层不是对象", b"[1, 2, 3]"),
                   ("空文件", b"")):
    page(use_index=True)
    io.open(INDEX, "wb").write(raw)
    got = page(use_index=True, fresh=True)
    want = page(use_index=False)
    check(label + "：结果与老路一致", got == want, first_diff(want, got))
page(use_index=True)
text = io.open(INDEX, encoding="utf-8").read()
io.open(INDEX, "w", encoding="utf-8").write(text[:len(text) // 2])
check("截断的索引文件：结果与老路一致",
      page(use_index=True, fresh=True) == page(use_index=False))
os.unlink(INDEX)
check("索引文件不存在：结果与老路一致",
      page(use_index=True, fresh=True) == page(use_index=False))

# 日志被截断（copytruncate）
page(use_index=True)
write_log(LINES[:20])
got = page(use_index=True, fresh=True)
want = page(use_index=False)
check("日志被截短：索引作废，结果按新内容重算", got == want,
      first_diff(want, got))

# 同尺寸重写（tail 签名要挡住）
write_log(LINES)
page(use_index=True)
size = os.path.getsize(USAGE_LOG)
text = io.open(USAGE_LOG, encoding="utf-8").read()
needle = '"total_tokens": 20'
pos = text.find(needle)
check("变异点找得到", pos >= 0, pos)
mutated = text[:pos] + '"total_tokens": 22' + text[pos + len(needle):]
check("变异保持日志字节数不变（size 校验对它是瞎的）",
      len(mutated) == size, (len(mutated), size))
io.open(USAGE_LOG, "w", encoding="utf-8", newline="\n").write(mutated)
want = page(use_index=False)
check("同尺寸重写确实改变了页内容（否则这条测不出东西）",
      want != GOLDEN["none"], "改写后与改写前相同")
got = page(use_index=True, fresh=True)
check("同尺寸重写：tail 签名挡住索引，结果按新内容重算", got == want,
      first_diff(want, got))
write_log(LINES)                            # 恢复基线日志

# 半行（写入中的碎片）：索引停在它之前，走老路，结果正确
append_log(['{"at": 1, "model": "m-a"'])
got = page(use_index=True, fresh=True)
want = page(use_index=False)
check("半行：结果与老路一致", got == want, first_diff(want, got))
write_log(LINES)

# 日志整个消失
page(use_index=True)
os.unlink(USAGE_LOG)
got = page(use_index=True, fresh=True)
want = page(use_index=False)
check("日志不存在：索引作废，结果为空", got == want, first_diff(want, got))
write_log(LINES)

# 手改过的行：realm 字段不是字符串（老路照常按 row_realm 判，索引不用它）
page(use_index=True)
append_log(['{"at": 9, "model": "m-a", "realm": 5, "account": "u1"}\n'])
got = page(use_index=True, fresh=True)
want = page(use_index=False)
check("realm 字段是数字的行：结果与老路一致", got == want, first_diff(want, got))
write_log(LINES)

# 写盘路径不可用：不崩、结果正确、不创建文件
os.environ["WB_RECENT_INDEX_MIN_BYTES"] = "0"
os.environ["WB_RECENT_INDEX_MIN_SECONDS"] = "0"
real_path = P.RECENT_INDEX_FILE
P.RECENT_INDEX_FILE = os.path.join(_TMP, "no-such-dir", "usage-recent-index.json")
try:
    got = page(use_index=True, fresh=True)
    want = page(use_index=False)
    check("写盘路径不可用：不抛错、结果与老路一致", got == want,
          first_diff(want, got))
    check("写盘路径不可用：没有创建任何文件",
          not os.path.exists(P.RECENT_INDEX_FILE))
finally:
    P.RECENT_INDEX_FILE = real_path
    os.environ.pop("WB_RECENT_INDEX_MIN_BYTES", None)
    os.environ.pop("WB_RECENT_INDEX_MIN_SECONDS", None)

# --------------------------------------------------------------------------
print()
print("[5] 归属门槛：老行的 realm 归属与活池绑定，池/磁盘不一致就不用索引")
# --------------------------------------------------------------------------
_ACCT_MTIME = [FROZEN - 2000000.0]


def write_account_file(uid, realm):
    path = os.path.join(ACCOUNTS_DIR, uid + ".json")
    io.open(path, "w", encoding="utf-8").write(json.dumps(
        {"uid": uid, "realm": realm, "accessToken": "", "nickname": uid}))
    # 指纹缓存键含 (size, mtime)：两次写入落在同一个刻度上会读不到新内容，
    # 拨到确定前进的整数秒上（checkpoint 套件同款）。
    _ACCT_MTIME[0] += 3600.0
    os.utime(path, (_ACCT_MTIME[0], _ACCT_MTIME[0]))


write_account_file("u9", "cn")
if os.path.exists(INDEX):
    os.unlink(INDEX)
# 池是空的、磁盘上有账号：折出来的归属冷进程复现不了 → realm 查询走老路
P.POOL = None
got, used = call_page(realm="cn", use_index=True, fresh=True)
want = page(use_index=False, realm="cn")
check("池没就绪：realm 页与老路一致", got == want, first_diff(want, got))
check("池没就绪：realm 页走了老路",
      used["backward"] > 0 and used["index"] == 0, used)
check("池没就绪：索引没有落盘（写下去别的进程也不敢用）",
      not os.path.exists(INDEX), os.listdir(USAGE_DIR))

# 池与磁盘一致：realm 页可以用索引
import wb_accounts            # noqa: E402
P.POOL = wb_accounts.AccountPool(ACCOUNTS_DIR)
P.POOL.load()
got, used = call_page(realm="cn", use_index=True, fresh=True)
want = page(use_index=False, realm="cn")
check("池与磁盘一致：realm 页与老路一致", got == want, first_diff(want, got))
check("池与磁盘一致：realm 页走了索引", used["index"] > 0, used)
check("池与磁盘一致：索引落盘了", os.path.exists(INDEX))

# 池与磁盘不一致（账号换了 realm 但池没重载）：realm 数字不许用索引
write_account_file("u9", "intl")
got, used = call_page(realm="cn", use_index=True, fresh=True)
want = page(use_index=False, realm="cn")
check("池没跟上磁盘：realm 页与老路一致", got == want, first_diff(want, got))
check("池没跟上磁盘：realm 页没走索引", used["index"] == 0, used)

# 磁盘归属改了、池也重载了：索引整份重折（老行的归属跟着变），结果仍一致
P.POOL = wb_accounts.AccountPool(ACCOUNTS_DIR)
P.POOL.load()
got, used = call_page(realm="cn", use_index=True, fresh=True)
want = page(use_index=False, realm="cn")
check("磁盘归属改了：realm 页与老路一致", got == want, first_diff(want, got))
check("磁盘归属改了：索引重折后仍然可用", used["index"] > 0, used)
write_account_file("u9", "cn")
P.POOL = None

# --------------------------------------------------------------------------
print()
print("[6] 4 万行日志：索引文件 <10KB，页内容与老路一致")
# --------------------------------------------------------------------------
BIG = os.path.join(_TMP, "big")
os.makedirs(BIG, exist_ok=True)
big_log = os.path.join(BIG, "usage.jsonl")
pad = "n" * 300
with io.open(big_log, "w", encoding="utf-8", newline="\n") as fh:
    for i in range(40000):
        r = row(i, realm="cn" if i % 31 == 0 else "intl",
                account="u%d" % (i % 40))
        r["note"] = pad
        fh.write(jline(r))
real_log = P.USAGE_LOG
P.USAGE_LOG = big_log
try:
    if os.path.exists(INDEX):
        os.unlink(INDEX)
    reset_index()
    reset_count()
    big_page, big_used = call_page(use_index=True)
    big_size = os.path.getsize(INDEX)
    check("4 万行日志的索引文件 <10KB", big_size < 10 * 1024, big_size)
    check("4 万行日志：第 1 页走了索引", big_used["index"] > 0, big_used)
    check("4 万行日志：第 1 页与老路一致",
          big_page == page(use_index=False), "不一致")
    check("4 万行日志：深页（第 300 页）与老路一致",
          page(limit=100, page_no=300, use_index=True)
          == page(limit=100, page_no=300, use_index=False), "不一致")
    check("4 万行日志：realm 页与老路一致",
          page(realm="cn", use_index=True) == page(realm="cn", use_index=False),
          "不一致")
    _, big_idx = read_bytes(lambda: page(limit=100, page_no=1, use_index=True))
    check("4 万行日志：第 1 页读取 ≤ 一段 + 页大小",
          big_idx <= 64 * 1024 + len(big_page), (big_idx, len(big_page)))
    _, big_deep = read_bytes(lambda: page(limit=100, page_no=300,
                                          use_index=True))
    # 一段 + 页本身，再按 64KB 分块向上取整：三段是上限（第 300 页 55KB 的
    # 行，起点在上一段里，所以实际读 128KB）。
    check("4 万行日志：第 300 页读取仍 ≤ 三段（与页深无关）",
          big_deep <= 3 * 64 * 1024, big_deep)
    _, big_deep_old = read_bytes(lambda: page(limit=100, page_no=300,
                                              use_index=False))
    check("4 万行日志：老路读第 300 页要读回几 MB（桩是活的）",
          big_deep_old > 4 * 1024 * 1024, big_deep_old)
    check("4 万行日志：深页的读取量比老路小两个数量级",
          big_deep_old > big_deep * 20, (big_deep_old, big_deep))
finally:
    P.USAGE_LOG = real_log
    if os.path.exists(INDEX):
        os.unlink(INDEX)
    reset_index()
    reset_count()
    os.environ.pop("WB_RECENT_INDEX", None)

# --------------------------------------------------------------------------
print()
print("[7] 开关：WB_RECENT_INDEX=0 时不读也不写索引")
# --------------------------------------------------------------------------
if os.path.exists(INDEX):
    os.unlink(INDEX)
got, used = call_page(use_index=False, fresh=True)
check("关掉索引：结果正确", got == GOLDEN["none"], first_diff(GOLDEN["none"], got))
check("关掉索引：走的是老路",
      used["backward"] > 0 and used["index"] == 0, used)
check("关掉索引：不写索引文件", not os.path.exists(INDEX),
      os.listdir(USAGE_DIR))
os.environ.pop("WB_RECENT_INDEX", None)

shutil.rmtree(_TMP, ignore_errors=True)
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
