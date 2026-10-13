/* wbc_api.h -- process-in-library ABI for the standalone aggregation engine.
 *
 * One engine, two faces: the CLI (main.c) parses argv into EngineOptions and
 * calls engine_run(); the shared library (wbc_api.c) fills the same struct
 * from a request JSON and calls the same entry point, so both emit the same
 * document.  The CLI keeps its byte-for-byte output and exit codes.
 *
 * Threading: not thread-safe; call wb_agg_run_json serially.
 * Lifetime: results are released with wb_agg_release; the pricing tables
 * stay cached for the process (that is the library's lifetime).
 */
#ifndef WBC_API_H
#define WBC_API_H

#include <stddef.h>

#include "wbc_util.h"   /* Buf */

/* Windows 的 DLL 只导出标注过的符号（其余平台默认全部可见）；构建共享库时
 * 定义 WBC_BUILD_DLL 即可（native/build_matrix.py 就是这么做的）。 */
#ifdef _WIN32
#  ifdef WBC_BUILD_DLL
#    define WBC_API __declspec(dllexport)
#  else
#    define WBC_API
#  endif
#else
#  define WBC_API __attribute__((visibility("default")))
#endif

/* ---- the JSON facade (wbc_api.c) ---- */

WBC_API const char *wb_agg_version(void);    /* "1" */

/* Run one aggregation.  Returns a malloc'd NUL-terminated JSON document
 * (release with wb_agg_release) or NULL on failure (text in wb_agg_error).
 * Repeated calls in one process are supported; apart from the pricing
 * cache no state crosses calls. */
WBC_API char *wb_agg_run_json(const char *request, size_t len);
WBC_API void wb_agg_release(char *result);
WBC_API const char *wb_agg_error(void);

/* ---- engine options / entry (main.c) ---- */

/* Sections, in emission order.  A section runs when its bit is set. */
#define WBC_SEC_SNAPSHOT    (1u << 0)
#define WBC_SEC_ANALYTICS   (1u << 1)
#define WBC_SEC_SERIES      (1u << 2)
#define WBC_SEC_BY_ACCOUNT  (1u << 3)
#define WBC_SEC_COUNT       (1u << 4)
#define WBC_SEC_DAILY       (1u << 5)
#define WBC_SEC_PERF        (1u << 6)
#define WBC_SEC_REMAINING   (1u << 7)
#define WBC_SEC_ALL         (WBC_SEC_SNAPSHOT | WBC_SEC_ANALYTICS | WBC_SEC_SERIES | \
                             WBC_SEC_BY_ACCOUNT | WBC_SEC_COUNT | WBC_SEC_DAILY | \
                             WBC_SEC_PERF | WBC_SEC_REMAINING)

/* The stderr summary the CLI prints (filled when `stats` is non-NULL). */
typedef struct {
    size_t n_lines, n_rows, n_bad;
    double seconds, load_s, parse_s, fold_s;
} EngineStats;

/* Everything main()'s option globals used to hold.  NULL pointers mean "the
 * CLI default": sibling files under the log's directory, "intl", "all". */
typedef struct {
    const char *log;              /* required */
    const char *policies;         /* NULL -> <log dir>/pricing-policies.jsonl */
    const char *timeline;         /* NULL -> <log dir>/pricing-timeline.jsonl */
    const char *pricing;          /* NULL -> no factory snapshot */
    const char *accounts;         /* NULL -> no accounts directory */
    const char *default_realm;    /* NULL -> "intl" */
    const char *realm;            /* snapshot filter; NULL/"all" = no filter */
    const char *series_realm;     /* NULL/"all" = no filter */
    const char *perf_realm;       /* NULL/"all" = no filter */
    const char *events;           /* NULL -> <log dir>/limit-events.jsonl */
    const char *estimate_daily;   /* NULL -> <log dir>/estimate-daily.json */
    double since, until;          /* CLI defaults 0.0 / 1e18 */
    int has_since, has_until;     /* 0 = the perf window ignores the bound */
    long long step;               /* CLI default 3600 */
    int daily_set;                /* 0 = daily_midnight = local today midnight */
    double daily_midnight;
    long long perf_sample;        /* CLI default 5000 */
    int now_set;                  /* 0 = now = time(NULL) */
    double now;
    unsigned sections;            /* WBC_SEC_* */
    int cache_pricing;            /* reuse the process-wide pricing cache */
    EngineStats *stats;           /* optional out; NULL = leave unset */
} EngineOptions;

/* Run the engine once, appending the JSON document and its trailing newline
 * to out_text (exactly the bytes the CLI writes to stdout / --out).
 * Returns 0, 1 (the log cannot be read) or 3 (out of memory at the
 * orchestration level; allocation failures inside the shared helpers keep
 * the CLI's exit(3) semantics).  CLI callers map the code to its exit code;
 * library callers turn it into wb_agg_error text. */
int engine_run(const EngineOptions *opts, Buf *out_text);

#endif /* WBC_API_H */
