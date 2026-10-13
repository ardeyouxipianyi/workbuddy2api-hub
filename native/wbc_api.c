/* wbc_api.c -- process-in-library ABI for the standalone aggregation engine.
 *
 * A thin JSON facade: parse the request with wbc_json.h, fill EngineOptions
 * exactly like main() fills it from argv, run engine_run() (main.c) and hand
 * back the same document the CLI writes to stdout.  Every CLI default is
 * reproduced: sibling pricing/events files under the log's directory, "intl"
 * default realm, all eight sections, local-today daily midnight, now =
 * time(NULL).
 *
 * Request (all keys but "log" optional):
 *   {"log":"...","policies":"...","timeline":"...","pricing":"...",
 *    "accounts":"...",
 *    "sections":["snapshot","analytics","series","by_account","count",
 *                "daily","perf","remaining"],
 *    "realm":"all","default_realm":"intl","since":0,"until":1e18,
 *    "has_since":false,"has_until":false,"step":3600,
 *    "series_realm":"all","daily_midnight":0,"perf_sample":5000,
 *    "perf_realm":"all","events":"...","estimate_daily":"...","now":0}
 * Unknown keys are ignored; a wrong type falls back to the CLI default
 * (number fields accept int and double; "sections" must be an array of
 * strings).  "daily_midnight" and "now", when present as numbers, are used
 * literally -- an absent key means "local midnight" / "time(NULL)", exactly
 * the CLI's unset flags.
 *
 * State: the pricing tables are cached process-wide (engine_run's cache,
 * keyed by the effective paths and their (mtime_ns, size) stamps); the
 * cache is never freed (the library's lifetime).  Everything else is
 * per-call.  Not thread-safe; call serially.
 */
#define _POSIX_C_SOURCE 200809L

#include <stdlib.h>
#include <string.h>

#include "wbc_api.h"
#include "wbc_json.h"
#include "wbc_util.h"

static char wb_err[512] = "no error";

static void err_set(const char *msg)
{
    size_t n = strlen(msg);
    if (n >= sizeof(wb_err)) n = sizeof(wb_err) - 1;
    memcpy(wb_err, msg, n);
    wb_err[n] = '\0';
}

static void err_set_path(const char *prefix, const char *path)
{
    size_t p = strlen(prefix);
    size_t m = path ? strlen(path) : 0;
    if (p >= sizeof(wb_err)) p = sizeof(wb_err) - 1;
    if (p + m > sizeof(wb_err) - 1) m = sizeof(wb_err) - 1 - p;
    memcpy(wb_err, prefix, p);
    if (m) memcpy(wb_err + p, path, m);
    wb_err[p + m] = '\0';
}

const char *wb_agg_version(void)
{
    return "1";
}

void wb_agg_release(char *result)
{
    free(result);
}

const char *wb_agg_error(void)
{
    return wb_err;
}

/* request readers: a missing key or a wrong type reads as the CLI default */
static const char *req_str(const JVal *obj, const char *key, const char *dflt)
{
    const JVal *v = jget(obj, key);
    if (v && jis_str(v)) {
        const char *s = jstr(v);
        if (s) return s;
    }
    return dflt;
}

static double req_num(const JVal *obj, const char *key, double dflt)
{
    const JVal *v = jget(obj, key);
    if (v && jis_num(v)) return jnum(v);
    return dflt;
}

static long long req_ll(const JVal *obj, const char *key, long long dflt)
{
    const JVal *v = jget(obj, key);
    double d;
    if (!v || !jis_num(v)) return dflt;
    d = jnum(v);
    if (d >= 9223372036854775808.0) return 9223372036854775807LL;
    if (d < -9223372036854775808.0) return -9223372036854775807LL - 1;
    return (long long)d;
}

static int req_flag(const JVal *obj, const char *key, int dflt)
{
    const JVal *v = jget(obj, key);
    if (!v) return dflt;
    if (jis_bool(v)) return jbool(v);
    if (jis_num(v)) return jnum(v) != 0.0;
    return dflt;
}

/* The "sections" array plays the CLI's --only string: each element enables
 * the section names it contains, so ["snapshot","analytics"] selects exactly
 * what --only snapshot,analytics selects (substring rules included).  An
 * absent or non-array value means all eight, like a missing --only. */
static unsigned sections_from_json(const JVal *doc)
{
    const JVal *v = jget(doc, "sections");
    unsigned sec = 0;
    size_t n, i;
    if (!v || !jis_arr(v)) return WBC_SEC_ALL;
    n = jlen(v);
    for (i = 0; i < n; i++) {
        const JVal *el = jat(v, i);
        const char *s;
        if (!el || !jis_str(el)) continue;
        s = jstr(el);
        if (!s) continue;
        if (strstr(s, "snapshot")) sec |= WBC_SEC_SNAPSHOT;
        if (strstr(s, "analytics")) sec |= WBC_SEC_ANALYTICS;
        if (strstr(s, "series")) sec |= WBC_SEC_SERIES;
        if (strstr(s, "by_account")) sec |= WBC_SEC_BY_ACCOUNT;
        if (strstr(s, "count")) sec |= WBC_SEC_COUNT;
        if (strstr(s, "daily")) sec |= WBC_SEC_DAILY;
        if (strstr(s, "perf")) sec |= WBC_SEC_PERF;
        if (strstr(s, "remaining")) sec |= WBC_SEC_REMAINING;
    }
    return sec;
}

char *wb_agg_run_json(const char *request, size_t len)
{
    JVal *doc;
    EngineOptions opts;
    Buf out;
    const JVal *v;
    int rc;
    char *result;

    err_set("no error");
    if (!request) {
        err_set("request is NULL");
        return NULL;
    }
    doc = jparse(request, len);
    if (!doc) {
        err_set_path("request: ", jparse_err());
        return NULL;
    }
    if (!jis_obj(doc)) {
        jfree(doc);
        err_set("request is not a JSON object");
        return NULL;
    }

    memset(&opts, 0, sizeof(opts));
    opts.log = req_str(doc, "log", NULL);
    if (!opts.log || !opts.log[0]) {
        jfree(doc);
        err_set("missing log");
        return NULL;
    }
    opts.policies = req_str(doc, "policies", NULL);
    opts.timeline = req_str(doc, "timeline", NULL);
    opts.pricing = req_str(doc, "pricing", NULL);
    opts.accounts = req_str(doc, "accounts", NULL);
    opts.default_realm = req_str(doc, "default_realm", "intl");
    opts.realm = req_str(doc, "realm", "all");
    opts.series_realm = req_str(doc, "series_realm", "all");
    opts.perf_realm = req_str(doc, "perf_realm", "all");
    opts.events = req_str(doc, "events", NULL);
    opts.estimate_daily = req_str(doc, "estimate_daily", NULL);
    opts.since = req_num(doc, "since", 0.0);
    opts.until = req_num(doc, "until", 1e18);
    opts.has_since = req_flag(doc, "has_since", 0);
    opts.has_until = req_flag(doc, "has_until", 0);
    opts.step = req_ll(doc, "step", 3600);
    opts.perf_sample = req_ll(doc, "perf_sample", 5000);
    v = jget(doc, "daily_midnight");
    if (v && jis_num(v)) {
        opts.daily_set = 1;
        opts.daily_midnight = jnum(v);
    }
    v = jget(doc, "now");
    if (v && jis_num(v)) {
        opts.now_set = 1;
        opts.now = jnum(v);
    }
    opts.sections = sections_from_json(doc);
    opts.cache_pricing = 1;      /* the library keeps the pricing tables cached */
    opts.stats = NULL;

    buf_init(&out);
    rc = engine_run(&opts, &out);
    if (rc != 0) {
        buf_free(&out);
        if (rc == 1) err_set_path("cannot read ", opts.log);
        else if (rc == 3) err_set("out of memory");
        else err_set("engine failure");
        jfree(doc);
        return NULL;
    }
    jfree(doc);

    /* the Buf owns a malloc'd, NUL-terminated buffer; hand it over */
    result = out.data;
    out.data = NULL;
    buf_free(&out);
    if (!result) {
        err_set("out of memory");
        return NULL;
    }
    return result;
}
