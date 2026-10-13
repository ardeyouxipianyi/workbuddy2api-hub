# wb-agg · 独立聚合引擎（可选加速件）

`usage.jsonl` 的八段聚合（snapshot / analytics / series / by_account / count /
daily / perf / remaining）用 C 写了一遍，编成两个产物：

- **`wb-agg`** —— 独立命令行程序，读一份请求 JSON、写一份结果 JSON，**完全静态**（无
  动态装载器、无 `DT_NEEDED`，实测见下）；
- **`libwb_agg.so`** —— 同一份代码编成共享库，走一个极小的 C ABI，由
  [`wb_native_agg.py`](../wb_native_agg.py) 用 ctypes 在进程内调用。

**它是可选的，而且当前未接线。** 网关照旧用纯 Python 折叠；`wb_proxy.py` 不 import
本模块（`tests/_test_native_agg.py` 钉住这条线）。库缺失、加载失败、ABI 版本不符或任何
一次调用出错，`wb_native_agg` 一律静默返回 `None`，调用方回退纯 Python —— 引擎没有把
网关带崩的路径。接线的收益与依据见下文「实测」一节，那是独立的一次改动。

## 构建

```sh
python native/build.py            # 需要 C99 编译器（默认 $CC，其次 cc）
python native/build.py --test     # 顺带跑 C 自测
python native/build.py --static   # CLI 静态链接（musl/alpine）
python native/build.py --cc clang
```

产物都落在 `native/` 下：`wb-agg`、`libwb_agg.so`（Windows 上 `wb_agg.dll`、
macOS 上 `libwb_agg.dylib`）与三个自测程序 `tests_json`、`tests_pricing`、
`probe_round`。

**依赖**：只需要一个 C99 编译器 —— 没有第三方库、不需要 Python 头文件、不链接
libpython。ELF 实测（musl x86_64）：

| 产物 | PT_INTERP | DT_NEEDED |
|---|---|---|
| `wb-agg`（CLI，`--static`） | 无 | **无** —— 全静态，目标机上没有任何运行时依赖 |
| `libwb_agg.so`（`-static-libgcc`，默认） | 无 | **`libc.so`** —— 只要系统 libc（OpenWrt 的 musl、桌面 glibc 都自带） |
| `libwb_agg.so`（不带该标志） | 无 | `libgcc_s.so.1`, `libc.so` —— 所以 `build.py` 默认加上 `-static-libgcc` |

两个 `.so` 变体都跑过夹具对拍，八段与 CLI 逐字节相等。

## 用法

CLI（结果 JSON 写 stdout，`--out` 可改到文件）：

```sh
native/wb-agg --log usage/usage.jsonl --only snapshot,analytics \
    --realm all --default-realm intl --pricing pricing/pricing.json \
    --daily-midnight 1791763200 --now 1791806400 > out.json
```

（`--only` 缺省即八段全出；完整参数表见 `main.c` 的 `usage()`。）

库（Python 侧，缺库即回退）：

```python
import wb_native_agg
payload = wb_native_agg.run({"log": "usage/usage.jsonl",
                             "sections": ["snapshot", "analytics"]})
if payload is None:
    payload = python_folds()      # 引擎不在场：照旧
```

C ABI 四个符号（`wbc_api.h`）：

```c
const char *wb_agg_version(void);                        /* 必须是 "1" */
char *wb_agg_run_json(const char *request, size_t len);  /* malloc 的 JSON 文本；失败 NULL */
void  wb_agg_release(char *result);                      /* 释放上面的返回值 */
const char *wb_agg_error(void);                          /* 最近一次错误文本（诊断用） */
```

请求 JSON 的键与 CLI 参数一一对应（`log`/`sections`/`realm`/`default_realm`/
`since`/`until`/`has_since`/`has_until`/`step`/`series_realm`/`daily_midnight`/
`perf_sample`/`perf_realm`/`policies`/`timeline`/`pricing`/`accounts`/`events`/
`estimate_daily`/`now`），返回就是 CLI 那份 JSON（八段 + `meta`）。

## 口径

C 侧是 Python 原始折叠函数的逐条移植（`_fold_usage_snapshot` / `_fold_usage_analytics`
/ `_perf_*` / `_fold_remaining` / `_pair_*` / `compute_row` / `band_for` …），刻意对齐的
几处容易踩的地方：

- **整数精确累加**：token 求和全整数走 int64，出现非整数才转 double —— 与 Python
  「全整数则精确」一致；credit/cost/百分位等按日志顺序逐行累加。
- **银行家舍入**：`round(v, 1)` / `round(v)` 用 `snprintf("%.1f"/"%.0f")` + `strtod`
  实现（C99 printf 就是「精确十进制 + 半到偶」）。真实日志抓到的边界：
  `105937/20 = 5296.85` 这个 double 略高于十进制平局，Python 进位到 5296.9，先 `×10`
  再半到偶的老写法会得到 5296.8 —— `probe_round.c` 钉住 17 个平局/边界。
- **真值语义**：`or 0`、`is not None`、`error` 键存在且真值、`outcome` 回退等一律照抄
  Python 判断。
- **坏行/半行**：`json.loads` 失败的整行跳过（count 例外，数所有非空行）；末尾无换行的
  半行不折 —— 与扫描器同规。
- **时区**：本地午夜由调用方算好传进来（`daily_midnight`），C 侧不猜 TZ；对拍统一
  `TZ=UTC`。
- 输出键序可能与 Python 不同，**对拍按 dict 语义比较**（与键序无关）。

## 验证

| 手段 | 内容 | 结果 |
|---|---|---|
| `native/parity.py` | 同一数据、同一时刻，C 输出 vs Python 原始折叠函数逐字段比 | 夹具 **948 叶子 0 差异**；真实日志 48,294 行 **2,191 叶子 0 差异** |
| `native/make_fixture.py` | 造夹具：三天用量、乱序行、三种 429 冷却形态、事件日志、归档预算、3 个带 modelCaps 的账号 | — |
| `native/bench_hybrid.py` | 四路基准（CLI / 库冷 / 库热 / Python）+ **库与 CLI 的 canonical JSON 逐字节比对** | 八段 **8/8 相等** |
| `native/tests_json.c` | 解析器/写出器 207 项（转义、代理对、深嵌套、坏 JSON 拒绝） | 全过 |
| `native/tests_pricing.c` | 价表 53 项（策略命中、时间线回填、bands、USD 折算、单位缺省） | 全过 |
| `native/probe_round.c` | 舍入 17 项（含上文的 5296.85 与 0.25/0.35/±2.5/-0.5 平局） | 全过 |
| `tests/_test_native_agg.py` | 回退契约（无库/坏库/假库）+ 包装逻辑（版本校验、NULL、释放一次、坏请求拦截、并发） | 17 项，CI 可跑 |

## 实测（x86_64 软路由，2 核，约 1.9 GB，`TZ=UTC`）

八段一次全出：

| 路径 | 48,294 行 / 28.3 MB | 150,000 行 / 87.5 MB |
|---|---:|---:|
| C 库（进程内） | 墙钟 **9.14 s** / CPU 2.53 s | 墙钟 22.14 s / CPU 7.78 s |
| C CLI（子进程） | 墙钟 **9.73 s** / CPU **2.29 s** | 墙钟 **18.74 s** / CPU **7.10 s** |
| Python 八路合计（冷态） | 墙钟 **193.3 s** | 墙钟 **1579.6 s** |
| 比值 | **≈21×** | **≈84×** |

热查询（增量状态命中时）Python 是 **38 ms**，C 任何形态都要 9 s 起 —— 快 200× 以上。
所以「取快」的结论是：**热路径留 Python，冷折（重启首个请求 / 状态判废 / 预热器 /
冷态窗口）才轮到 C 库**，一次扫描出八段，不背逐段重复解析。两个已知的反向点：
perf 单查在 48k 档 Python 更快（只读尾部 `perf_sample` 行，1.86 s vs C 3.1–4.5 s 墙钟，
150k 档 C 反超）；count 单查 C 为别的段多付了一次整份解析。

## 边界与未做

- remaining 的**归档写盘**（`estimate-daily.json` 跨天落盘）与 checkpoint 不搬：C 版只读
  归档、不写，批处理语义。
- 库内 OOM 的深部 helper 仍 `exit(3)`（与 CLI 同一条代码路径）；跨调用行字符串 arena
  只增不回收，长驻进程每次调用涨 KB 级。
- 窗口切片（since/until）已在库里支持，但**快照/分析段的增量状态（checkpoint）不在
  C 侧** —— 那是 Python 侧的缓存机制，不是计算。
