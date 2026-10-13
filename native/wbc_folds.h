/* Aggregation folds for the standalone engine.
 *
 * Numeric policy: every token field accumulates through Num (exact int64 while
 * all contributions are integers, float once one is not); credit / cost / ttft
 * / speed / elapsed accumulate as plain doubles in log order, exactly like the
 * Python folds.  Counters are int64.  Output is JSON written with the caller's
 * JW; key order is insertion order and comparison against Python is done with
 * dict semantics (order-insensitive).
 */
#ifndef WBC_FOLDS_H
#define WBC_FOLDS_H

#include "wbc_json.h"
#include "wbc_pricing.h"
#include "wbc_row.h"
#include "wbc_util.h"

/* ---- snapshot (wb_proxy._fold_usage_snapshot / _fold_cost) ----
 * Output:
 * {"requests":N,"errors":N,"prompt_tokens":N,"completion_tokens":N,
 *  "reasoning_tokens":N,"cached_tokens":N,"total_tokens":N,"credit":f,
 *  "cost_cny":f,"cost_missing":{model:count},
 *  "by_model":{m:{requests,errors,<tokens>,credit,cost_cny,cost_missing,accounts}},
 *  "by_model_realm":{m:{realm:{...同 by_model 桶...}}},
 *  "by_model_acct":{m:{realm:{acct:{...同 by_model 桶...}}}}}
 * 规则见 CONTRACT.md。第 5 个参数是 realm 过滤器：NULL/""/"all" = 全部；
 * 没有 realm 的行一律回落到 "intl"（wb_proxy.row_realm 的最后一档，与过滤器
 * 取值无关）。since/until 是窗口边界，取值口径同 _fold_usage_snapshot：
 * 0 = 不设该边界，上界含端点。
 */
void fold_snapshot(const Row *rows, size_t n, const Pricing *pr,
                   const Map *accounts, const char *realm_filter,
                   double since, double until, JW *out);

/* ---- analytics (wb_proxy._fold_usage_analytics / _feed_analytics /
 * _bump_analytics_models) ----
 * Output (all-time half only):
 * {"summary":{<stat>},
 *  "accts":{uid:{"uid":u,"nickname":u,"realm":field,"domain":"","all_time":{<stat>},
 *                "all_models":{m:{requests,tokens,reasoning,cost_cny}}}},
 *  "models":{m:{"model":m,"all_time":{<stat>}}},
 *  "keys":{k:{"key":k,"all_time":{<stat>},"all_models":{...},
 *             "realms":{realm:count},"last_at":f}}}
 * <stat> = requests,errors,prompt_tokens,completion_tokens,reasoning_tokens,
 *          cached_tokens,total_tokens,credit,cost_cny,ttft_sum,ttft_n,
 *          speed_sum,speed_n,elapsed_sum,elapsed_n。
 * key 分桶：行无 "key" 键 → "__before_keys__"；有但空 → "__no_key__"。
 * 第 5 个参数是行归属回退的缺省 realm（无过滤器；等价 Python realm=None）。
 */
void fold_analytics(const Row *rows, size_t n, const Pricing *pr,
                    const Map *accounts, const char *default_realm, JW *out);

/* ---- series cold scan (wb_proxy._usage_timeseries_scan) ----
 * Output: {"bucket_seconds":step,"since":lo,"until":hi,
 *          "series":[{at,requests,errors,<5 tokens>,credit}...按 key 升序],
 *          "credits":[{at,iso,model,account,credit,total_tokens}...]}
 * credits = 窗口内 credit>0 的前 50 条，(at 倒序, 同 at 日志顺序)。
 * realm 传 NULL=全部。lo/hi 为两端包含。
 */
void fold_series(const Row *rows, size_t n, double lo, double hi, long long step,
                 const char *realm, const Map *accounts,
                 const char *default_realm, JW *out);

/* ---- by-account (wb_proxy._fold_usage_by_account) ----
 * Output: {acct:{"account":acct,"requests":N,<5 tokens>,"models":{m:count}}}
 * 只跳过 error 为真的行（不看 outcome）。
 */
void fold_by_account(const Row *rows, size_t n, JW *out);

/* ---- count (wb_proxy._count_usage_line) ----
 * lines/lens 是原始行文本（已 strip，非空）。输出：
 * {"n":N,"n_realm":{realm:count}}
 * n 数所有传入行；每处 `"realm"` 键文本按两种间距各计一次；
 * 完全没有该键的行解析后走 row_realm 回退（解析失败只进 n）。
 * 第 5 个参数是行归属回退的缺省 realm。
 */
void fold_count(const char *const *lines, const size_t *lens, size_t n,
                const Map *accounts, const char *default_realm, JW *out);

/* Streaming count: same rules, no line array kept (keeps peak memory down on
 * long logs).  count_finish writes {"n":N,"n_realm":{...}} and frees the
 * counter. */
typedef struct WBCount WBCount;
WBCount *count_begin(const Map *accounts, const char *default_realm);
void count_feed(WBCount *counter, const char *line, size_t len);
void count_finish(WBCount *counter, JW *out);

/* ---- daily (wb_proxy._scan_daily_usage) ----
 * Output: {"tokens":{uid:N},"credits":{uid:f},"models":{uid:{model:tokens}}}
 * 只折 at >= midnight 的行；client_aborted 跳过；无 account 跳过。
 */
void fold_daily(const Row *rows, size_t n, double midnight, JW *out);

#endif
