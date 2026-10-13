/* wb-agg: standalone aggregation engine for the usage log (no Python at run time).
 *
 *   wb-agg --log usage.jsonl [--policies FILE] [--timeline FILE] [--pricing FILE]
 *          [--accounts DIR] [--default-realm intl]
 *          [--since X --until Y --step N] [--series-realm all]
 *          [--daily-midnight X] [--only snapshot,analytics,...] [--out FILE]
 *
 * The same engine backs the shared library (wbc_api.c): main() parses argv
 * into EngineOptions and calls engine_run(); the library fills the same
 * struct from a request JSON and calls the same entry point.  The CLI's
 * output bytes and exit codes are unchanged.
 */
#define _POSIX_C_SOURCE 200809L

#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "wbc_port.h"
#include <sys/stat.h>
#include <time.h>

#include "wbc_api.h"
#include "wbc_folds.h"
#include "wbc_json.h"
#include "wbc_pricing.h"
#include "wbc_row.h"
#include "wbc_util.h"

/* The account shape the remaining payload reads (see PERF_REMAINING_SPEC.md);
 * the module repeats this layout. */
struct rm_account {
    const char *uid;
    const char *nickname;
    const char *realm;
    int enabled;
    const JVal *model_caps;
};

static char *sibling_path(const char *base, const char *name)
{
    const char *slash = strrchr(base, '/');
    const char *back = strrchr(base, '\\');
    size_t dir_len;
    char *out;
    if (back && (!slash || back > slash)) slash = back;
    dir_len = slash ? (size_t)(slash - base) + 1 : 0;
    out = (char *)malloc(dir_len + strlen(name) + 1);
    if (!out) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    memcpy(out, base, dir_len);
    strcpy(out + dir_len, name);
    return out;
}

static char *read_file(const char *path, size_t *len_out)
{
    FILE *fh = fopen(path, "rb");
    char *data;
    long size;
    size_t got;
    if (!fh) return NULL;
    if (fseek(fh, 0, SEEK_END) != 0) { fclose(fh); return NULL; }
    size = ftell(fh);
    if (size < 0) { fclose(fh); return NULL; }
    rewind(fh);
    data = (char *)malloc((size_t)size + 1);
    if (!data) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    got = fread(data, 1, (size_t)size, fh);
    fclose(fh);
    data[got] = '\0';
    *len_out = got;
    return data;
}

static int is_space(char c)
{
    return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\v' || c == '\f';
}

static double local_midnight_now(void)
{
    time_t now = time(NULL);
    struct tm lt;
    struct tm copy;
    time_t result;
    lt = *localtime(&now);
    copy = lt;
    copy.tm_hour = 0;
    copy.tm_min = 0;
    copy.tm_sec = 0;
    copy.tm_isdst = -1;
    result = mktime(&copy);
    return (double)result;
}

/* Load accounts from a directory of JSON files.  Two maps come out of one
 * pass: uid -> realm string (row attribution) and uid -> rm_account (the
 * remaining payload).  The parsed trees stay alive for model_caps and are
 * freed at exit through `keep`. */
static void load_accounts(const char *dir, Map *accounts, Map *rm_accounts,
                          JVal ***keep, size_t *keep_n, size_t *keep_cap)
{
    DIR *dh = opendir(dir);
    struct dirent *entry;
    if (!dh) return;
    while ((entry = readdir(dh)) != NULL) {
        char path[4096];
        size_t len = strlen(entry->d_name);
        char *blob;
        size_t blob_len = 0;
        JVal *doc;
        const char *realm, *uid, *nickname;
        char stem[512];
        struct rm_account *acct;
        const JVal *enabled;
        if (len < 6 || strcmp(entry->d_name + len - 5, ".json") != 0) continue;
        snprintf(path, sizeof(path), "%s/%s", dir, entry->d_name);
        blob = read_file(path, &blob_len);
        if (!blob) continue;
        doc = jparse(blob, blob_len);
        free(blob);
        if (!doc) continue;
        if (*keep_n == *keep_cap) {
            *keep_cap = *keep_cap ? *keep_cap * 2 : 8;
            *keep = (JVal **)realloc(*keep, sizeof(JVal *) * (*keep_cap));
            if (!*keep) {
                fprintf(stderr, "wb-agg: out of memory\n");
                exit(3);
            }
        }
        (*keep)[(*keep_n)++] = doc;
        realm = jstr(jget(doc, "realm"));
        if (realm && realm[0]) {
            uid = jstr(jget(doc, "uid"));
            size_t stem_len = len - 5;
            if (stem_len >= sizeof(stem)) stem_len = sizeof(stem) - 1;
            memcpy(stem, entry->d_name, stem_len);
            stem[stem_len] = '\0';
            map_put(accounts, (uid && uid[0]) ? uid : stem, strdup(realm));
        }
        /* the remaining view wants every account the file names, realm or not */
        uid = jstr(jget(doc, "uid"));
        if (!uid || !uid[0]) {
            size_t stem_len = len - 5;
            if (stem_len >= sizeof(stem)) stem_len = sizeof(stem) - 1;
            memcpy(stem, entry->d_name, stem_len);
            stem[stem_len] = '\0';
            uid = stem;
        }
        nickname = jstr(jget(doc, "nickname"));
        enabled = jget(doc, "enabled");
        acct = (struct rm_account *)calloc(1, sizeof(*acct));
        if (!acct) {
            fprintf(stderr, "wb-agg: out of memory\n");
            exit(3);
        }
        acct->uid = strdup(uid);
        acct->nickname = nickname ? strdup(nickname) : NULL;
        acct->realm = realm ? strdup(realm) : "";
        acct->enabled = (!enabled || jis_null(enabled)) ? 1
                        : (jis_bool(enabled) ? jbool(enabled) : 1);
        acct->model_caps = jget(doc, "modelCaps");
        map_put(rm_accounts, uid, acct);
    }
    closedir(dh);
}

static void usage(void)
{
    fprintf(stderr,
        "usage: wb-agg --log FILE [--policies FILE] [--timeline FILE] [--pricing FILE]\n"
        "              [--accounts DIR] [--default-realm NAME] [--realm all|intl|cn]\n"
        "              [--since X] [--until Y] [--step N] [--series-realm NAME]\n"
        "              [--daily-midnight X] [--only LIST] [--out FILE]\n"
        "              [--perf-sample N] [--perf-realm NAME] [--events FILE]\n"
        "              [--estimate-daily FILE] [--now X]\n");
}

/* ---- perf line ring: the last `sample` non-empty lines, in order ---- */
static char **perf_lines;
static size_t *perf_lens;
static size_t perf_cap, perf_n, perf_start;

static void perf_line_push(const char *line, size_t len)
{
    char *copy;
    size_t slot;
    if (perf_cap == 0) return;
    copy = (char *)malloc(len + 1);
    if (!copy) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    memcpy(copy, line, len);
    copy[len] = '\0';
    if (perf_n < perf_cap) {
        slot = (perf_start + perf_n) % perf_cap;
        perf_n++;
    } else {
        slot = perf_start;
        perf_start = (perf_start + 1) % perf_cap;
        free(perf_lines[slot]);
    }
    perf_lines[slot] = copy;
    perf_lens[slot] = len;
}

static void perf_lines_free(void)
{
    size_t i;
    if (!perf_lines) return;
    for (i = 0; i < perf_cap; i++) free(perf_lines[i]);
    free(perf_lines);
    free(perf_lens);
    perf_lines = NULL;
    perf_lens = NULL;
    perf_cap = perf_n = perf_start = 0;
}

/* The remaining state and the account view it reads. */
struct Remaining;
extern struct Remaining *remaining_new(void);
extern void remaining_fold_json(struct Remaining *st, const JVal *obj);
extern void remaining_merge_event_json(struct Remaining *st, const JVal *ev);
extern void remaining_free(struct Remaining *st);
extern void remaining_payload(const struct Remaining *st, double now,
                              const Map *accounts, const JVal *daily, JW *out);
extern void fold_perf(const char *const *lines, const size_t *lens, size_t n,
                      long long sample, const char *realm_filter,
                      int has_since, double since, int has_until, double until,
                      const Map *accounts, const char *default_realm, JW *out);

static struct Remaining *rm_state;

static void handle_line(const char *raw, size_t len, Row **rows_io,
                        size_t *cap_io, size_t *n_rows, size_t *n_lines,
                        size_t *n_bad, WBCount *counter)
{
    size_t start = 0, stop = len;
    JVal *tree;
    while (start < stop && is_space(raw[start])) start++;
    while (stop > start && is_space(raw[stop - 1])) stop--;
    if (stop <= start) return;
    (*n_lines)++;
    if (counter) count_feed(counter, raw + start, stop - start);
    if (perf_cap) perf_line_push(raw + start, stop - start);
    tree = jparse(raw + start, stop - start);
    if (tree) {
        if (jis_obj(tree)) {
            if (rm_state) remaining_fold_json(rm_state, tree);
            if (*n_rows == *cap_io) {
                *cap_io *= 2;
                *rows_io = (Row *)realloc(*rows_io, sizeof(Row) * (*cap_io));
                if (!*rows_io) {
                    fprintf(stderr, "wb-agg: out of memory\n");
                    exit(3);
                }
            }
            row_from_json(&(*rows_io)[*n_rows], tree);
            row_own(&(*rows_io)[*n_rows]);
            (*n_rows)++;
        } else {
            (*n_bad)++;
        }
        jfree(tree);
    } else {
        (*n_bad)++;
    }
}

/* Stream the log in chunks instead of holding the whole file: a long log is
 * tens of MB and the rows are all the engine needs to keep.  A trailing
 * fragment without a newline is dropped, exactly like the incremental scan
 * leaves a partial tail.  Each row's JSON tree is freed right after its
 * fields are copied into the Row.  Returns 0, or -1 when the file cannot be
 * read. */
static int stream_log(const char *path, Row **rows_io, size_t *cap_io,
                      size_t *n_rows, size_t *n_lines, size_t *n_bad,
                      WBCount *counter, char **partial_out, size_t *partial_len_out)
{
    FILE *fh = fopen(path, "rb");
    size_t chunk_cap = 1u << 20;
    char *chunk;
    char *carry = NULL;
    size_t carry_len = 0, carry_cap = 0;
    if (partial_out) *partial_out = NULL;
    if (partial_len_out) *partial_len_out = 0;
    if (!fh) return -1;
    chunk = (char *)malloc(chunk_cap);
    if (!chunk) {
        fclose(fh);
        return -1;
    }
    for (;;) {
        size_t got = fread(chunk, 1, chunk_cap, fh);
        char *p = chunk, *end = chunk + got;
        if (got == 0) break;
        while (p < end) {
            char *nl = (char *)memchr(p, '\n', (size_t)(end - p));
            const char *line;
            size_t len;
            if (nl == NULL) {
                size_t add = (size_t)(end - p);
                if (carry_len + add > carry_cap) {
                    carry_cap = (carry_len + add) * 2 + 64;
                    carry = (char *)realloc(carry, carry_cap);
                    if (!carry) {
                        fclose(fh);
                        free(chunk);
                        return -1;
                    }
                }
                memcpy(carry + carry_len, p, add);
                carry_len += add;
                break;
            }
            if (carry_len > 0) {
                size_t add = (size_t)(nl - p);
                if (carry_len + add > carry_cap) {
                    carry_cap = (carry_len + add) * 2 + 64;
                    carry = (char *)realloc(carry, carry_cap);
                    if (!carry) {
                        fclose(fh);
                        free(chunk);
                        return -1;
                    }
                }
                memcpy(carry + carry_len, p, add);
                carry_len += add;
                line = carry;
                len = carry_len;
                carry_len = 0;
            } else {
                line = p;
                len = (size_t)(nl - p);
            }
            handle_line(line, len, rows_io, cap_io, n_rows, n_lines, n_bad, counter);
            p = nl + 1;
        }
        if (got < chunk_cap) break;
    }
    fclose(fh);
    free(chunk);
    if (partial_out && carry_len > 0) {
        /* the trailing fragment counts as the sample's last line (it is the
         * line the tail reader would see), stripped like every other line */
        size_t start = 0, stop = carry_len;
        while (start < stop && is_space(carry[start])) start++;
        while (stop > start && is_space(carry[stop - 1])) stop--;
        if (stop > start) {
            size_t n = stop - start;
            char *copy = (char *)malloc(n + 1);
            if (!copy) {
                fprintf(stderr, "wb-agg: out of memory\n");
                exit(3);
            }
            memcpy(copy, carry + start, n);
            copy[n] = '\0';
            *partial_out = copy;
            *partial_len_out = n;
        }
    }
    free(carry);
    return 0;
}

/* ---- process-wide pricing cache (library mode only) ----
 * wb_agg_run_json calls engine_run repeatedly; re-parsing the 522 KB
 * timeline on every call dominated the mix.  The cache keeps one Pricing
 * keyed by the effective (policies, timeline, doc) paths and their
 * (mtime_ns, size) stamps; unchanged stamps reuse the old pointer.  CLI
 * runs never set opts->cache_pricing, so the CLI path is exactly the old
 * load-and-free.  The cached object lives for the process (the library's
 * lifetime) and is never freed. */
static Pricing *cache_pricing_obj;
static char *cache_pricing_paths[3];
static long long cache_pricing_stamps[3][2];   /* [file][0]=mtime_ns [file][1]=size */
static int cache_pricing_valid;

static char *dup_or_exit(const char *s)
{
    size_t n;
    char *out;
    if (!s) return NULL;
    n = strlen(s);
    out = (char *)malloc(n + 1);
    if (!out) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    memcpy(out, s, n + 1);
    return out;
}

/* A missing file stamps as (0, -1), so "still missing" compares equal and
 * "appeared/disappeared/rewritten" compares different. */
static void file_stamp(const char *path, long long *mtime_ns, long long *size)
{
    struct stat st;
    *mtime_ns = 0;
    *size = -1;
    if (path && stat(path, &st) == 0) {
        *mtime_ns = WBC_STAT_MTIME_NS(st);
        *size = (long long)st.st_size;
    }
}

static Pricing *pricing_cached(const char *policies, const char *timeline,
                               const char *doc)
{
    const char *paths[3];
    long long stamps[3][2];
    int i, same = 1;
    paths[0] = policies;
    paths[1] = timeline;
    paths[2] = doc;
    for (i = 0; i < 3; i++) file_stamp(paths[i], &stamps[i][0], &stamps[i][1]);
    if (cache_pricing_valid) {
        for (i = 0; i < 3; i++) {
            const char *a = cache_pricing_paths[i];
            const char *b = paths[i];
            if ((a == NULL) != (b == NULL)) same = 0;
            else if (a && strcmp(a, b)) same = 0;
            else if (stamps[i][0] != cache_pricing_stamps[i][0] ||
                     stamps[i][1] != cache_pricing_stamps[i][1]) same = 0;
        }
        if (same) return cache_pricing_obj;
        pricing_free(cache_pricing_obj);
        cache_pricing_obj = NULL;
        for (i = 0; i < 3; i++) {
            free(cache_pricing_paths[i]);
            cache_pricing_paths[i] = NULL;
        }
        cache_pricing_valid = 0;
    }
    cache_pricing_obj = pricing_load(policies, timeline, doc);
    for (i = 0; i < 3; i++)
        cache_pricing_paths[i] = dup_or_exit(paths[i]);
    memcpy(cache_pricing_stamps, stamps, sizeof(stamps));
    cache_pricing_valid = 1;
    return cache_pricing_obj;
}

/* ---- the orchestration the CLI and the library share ----
 * Everything main() used to do between argv parsing and writing the output;
 * the same option semantics, the same fold order, the same meta block.
 * Returns 0, 1 (the log cannot be read) or 3 (out of memory at this level;
 * the deep helpers keep their exit(3) CLI semantics).  On success the JSON
 * text plus its trailing newline is appended to out_text. */
int engine_run(const EngineOptions *opts, Buf *out_text)
{
    int i;
    Row *rows = NULL;
    WBCount *counter = NULL;
    size_t n_lines = 0, n_rows = 0, n_bad = 0;
    Map accounts;
    Pricing *pricing = NULL;
    char *policies_path = NULL, *timeline_path = NULL;
    char *events_sib = NULL, *daily_sib = NULL;
    JW *out = NULL;
    char *text = NULL;
    size_t text_len = 0;
    /* 单调时钟的读数（秒）；跨平台取法见 wbc_port.h。只喂 meta 的计时字段。 */
    double t0 = 0.0, t1 = 0.0, t_load = 0.0, t_parse = 0.0;
    double seconds = 0.0, load_s = 0.0, parse_s = 0.0, fold_s = 0.0;
    unsigned sec;
    char *partial_line = NULL;
    size_t partial_len = 0;
    JVal *daily_doc = NULL;
    JVal **keep = NULL;
    size_t keep_n = 0, keep_cap = 0;
    Map rm_accounts;
    double now_ts;
    double daily_midnight;
    const char *default_realm;
    const char *log;
    int rc;

    if (!opts || !out_text || !opts->log || !opts->log[0]) return 1;
    log = opts->log;
    default_realm = (opts->default_realm && opts->default_realm[0])
                    ? opts->default_realm : "intl";
    sec = opts->sections & WBC_SEC_ALL;

    /* one call must never see the previous call's per-run state */
    perf_lines_free();
    rm_state = NULL;

    if (!opts->policies) policies_path = sibling_path(log, "pricing-policies.jsonl");
    if (!opts->timeline) timeline_path = sibling_path(log, "pricing-timeline.jsonl");

    t0 = wbc_now_seconds();
    map_init(&accounts);
    map_init(&rm_accounts);
    if (opts->accounts) load_accounts(opts->accounts, &accounts, &rm_accounts,
                                      &keep, &keep_n, &keep_cap);
    if (opts->cache_pricing) {
        pricing = pricing_cached(opts->policies ? opts->policies : policies_path,
                                 opts->timeline ? opts->timeline : timeline_path,
                                 opts->pricing);
    } else {
        pricing = pricing_load(opts->policies ? opts->policies : policies_path,
                               opts->timeline ? opts->timeline : timeline_path,
                               opts->pricing);
    }
    t_load = wbc_now_seconds();

    if ((sec & WBC_SEC_PERF) && opts->perf_sample > 0) {
        perf_cap = (size_t)opts->perf_sample;
        perf_lines = (char **)calloc(perf_cap, sizeof(char *));
        perf_lens = (size_t *)calloc(perf_cap, sizeof(size_t));
        if (!perf_lines || !perf_lens) {
            rc = 3;
            goto cleanup;
        }
    }
    if (sec & WBC_SEC_REMAINING) rm_state = remaining_new();

    {
        size_t capacity = 4096;
        rows = (Row *)malloc(sizeof(Row) * capacity);
        if (sec & WBC_SEC_COUNT) counter = count_begin(&accounts, default_realm);
        if (!rows) {
            rc = 3;
            goto cleanup;
        }
        if (stream_log(log, &rows, &capacity, &n_rows, &n_lines, &n_bad,
                       counter, (sec & WBC_SEC_PERF) ? &partial_line : NULL,
                       (sec & WBC_SEC_PERF) ? &partial_len : NULL) != 0) {
            rc = 1;
            goto cleanup;
        }
        if ((sec & WBC_SEC_PERF) && partial_line) perf_line_push(partial_line, partial_len);
    }
    t_parse = wbc_now_seconds();

    if (sec & WBC_SEC_REMAINING) {
        const char *events_path = opts->events ? opts->events
                                  : (events_sib = sibling_path(log, "limit-events.jsonl"));
        const char *daily_path = opts->estimate_daily ? opts->estimate_daily
                                 : (daily_sib = sibling_path(log, "estimate-daily.json"));
        size_t blob_len = 0;
        char *blob = read_file(events_path, &blob_len);
        if (blob) {
            char *p = blob, *end = blob + blob_len;
            while (p < end) {
                char *nl = (char *)memchr(p, '\n', (size_t)(end - p));
                size_t len = nl ? (size_t)(nl - p) : (size_t)(end - p);
                JVal *doc = jparse(p, len);
                if (doc) {
                    if (jis_obj(doc)) remaining_merge_event_json(rm_state, doc);
                    if (keep_n == keep_cap) {
                        keep_cap = keep_cap ? keep_cap * 2 : 8;
                        keep = (JVal **)realloc(keep, sizeof(JVal *) * keep_cap);
                        if (!keep) {
                            rc = 3;
                            goto cleanup;
                        }
                    }
                    keep[keep_n++] = doc;
                }
                if (!nl) break;
                p = nl + 1;
            }
            free(blob);
        }
        blob = read_file(daily_path, &blob_len);
        if (blob) {
            daily_doc = jparse(blob, blob_len);
            free(blob);
        }
    }
    now_ts = opts->now_set ? opts->now : (double)time(NULL);
    daily_midnight = opts->daily_set ? opts->daily_midnight : local_midnight_now();

    out = jw_new();
    jw_obj_begin(out);
    if (sec & WBC_SEC_SNAPSHOT) {
        /* the 5th argument of fold_snapshot is the realm FILTER (NULL/""/all
         * = every realm), matching usage_snapshot(realm="all") on the Python
         * side of the parity harness. */
        jw_key(out, "snapshot");
        fold_snapshot(rows, n_rows, pricing, &accounts, opts->realm, out);
    }
    if (sec & WBC_SEC_ANALYTICS) {
        jw_key(out, "analytics");
        fold_analytics(rows, n_rows, pricing, &accounts, default_realm, out);
    }
    if (sec & WBC_SEC_SERIES) {
        const char *realm = (!opts->series_realm || !strcmp(opts->series_realm, "all"))
                            ? NULL : opts->series_realm;
        jw_key(out, "series");
        fold_series(rows, n_rows, opts->since, opts->until, opts->step, realm,
                    &accounts, default_realm, out);
    }
    if (sec & WBC_SEC_BY_ACCOUNT) {
        jw_key(out, "by_account");
        fold_by_account(rows, n_rows, out);
    }
    if (sec & WBC_SEC_COUNT) {
        jw_key(out, "count");
        count_finish(counter, out);
        counter = NULL;
    }
    if (sec & WBC_SEC_DAILY) {
        jw_key(out, "daily");
        fold_daily(rows, n_rows, daily_midnight, out);
    }
    if (sec & WBC_SEC_PERF) {
        const char *realm = (!opts->perf_realm || !strcmp(opts->perf_realm, "all"))
                            ? NULL : opts->perf_realm;
        const char **plines = NULL;
        size_t *plens = NULL;
        size_t i2;
        if (perf_n > 0) {
            plines = (const char **)malloc(sizeof(char *) * perf_n);
            plens = (size_t *)malloc(sizeof(size_t) * perf_n);
            if (!plines || !plens) {
                rc = 3;
                goto cleanup;
            }
            for (i2 = 0; i2 < perf_n; i2++) {
                size_t idx = (perf_start + i2) % perf_cap;
                plines[i2] = perf_lines[idx];
                plens[i2] = perf_lens[idx];
            }
        }
        jw_key(out, "perf");
        fold_perf(plines, plens, perf_n, opts->perf_sample, realm,
                  opts->has_since, opts->since, opts->has_until, opts->until,
                  &accounts, default_realm, out);
        free(plines);
        free(plens);
    }
    if (sec & WBC_SEC_REMAINING) {
        jw_key(out, "remaining");
        remaining_payload(rm_state, now_ts, &rm_accounts, daily_doc, out);
    }
    t1 = wbc_now_seconds();
    seconds = t1 - t0;
    load_s = t_load - t0;
    parse_s = t_parse - t_load;
    fold_s = seconds - load_s - parse_s;
    jw_key(out, "meta");
    jw_obj_begin(out);
    jw_key(out, "log"); jw_str(out, log);
    jw_key(out, "lines"); jw_int(out, (long long)n_lines);
    jw_key(out, "rows"); jw_int(out, (long long)n_rows);
    jw_key(out, "bad_json"); jw_int(out, (long long)n_bad);
    jw_key(out, "daily_midnight"); jw_double(out, daily_midnight);
    jw_key(out, "c_seconds"); jw_double(out, seconds);
    jw_key(out, "load_seconds"); jw_double(out, load_s);
    jw_key(out, "parse_seconds"); jw_double(out, parse_s);
    jw_key(out, "fold_seconds"); jw_double(out, fold_s);
    {
        double cpu = 0.0;
        long long peak_rss_kib = 0;
        if (wbc_process_usage(&cpu, &peak_rss_kib)) {
            jw_key(out, "cpu_seconds"); jw_double(out, cpu);
            jw_key(out, "peak_rss_kib"); jw_int(out, peak_rss_kib);
        }
    }
    jw_obj_end(out);
    jw_obj_end(out);

    text = jw_take(out, &text_len);
    jw_free(out);
    out = NULL;
    if (text) {
        buf_put(out_text, text, text_len);
        buf_putc(out_text, '\n');
        free(text);
        text = NULL;
    }

    if (opts->stats) {
        opts->stats->n_lines = n_lines;
        opts->stats->n_rows = n_rows;
        opts->stats->n_bad = n_bad;
        opts->stats->seconds = seconds;
        opts->stats->load_s = load_s;
        opts->stats->parse_s = parse_s;
        opts->stats->fold_s = fold_s;
    }
    rc = 0;

cleanup:
    for (i = 0; i < (int)n_rows; i++) row_free(&rows[i]);
    free(rows);
    if (out) jw_free(out);
    free(text);
    perf_lines_free();
    free(partial_line);
    if (rm_state) {
        remaining_free(rm_state);
        rm_state = NULL;
    }
    for (i = 0; i < (int)keep_n; i++) jfree(keep[i]);
    free(keep);
    if (daily_doc) jfree(daily_doc);
    map_free(&rm_accounts);
    map_free(&accounts);
    if (!opts->cache_pricing) pricing_free(pricing);
    free(events_sib);
    free(daily_sib);
    free(policies_path);
    free(timeline_path);
    return rc;
}

int main(int argc, char **argv)
{
    int i;
    EngineOptions opts;
    EngineStats stats;
    const char *only = NULL;
    const char *out_path = NULL;
    Buf out;
    int rc;

    /* Windows 上 stdout 默认是文本模式，会把 \n 翻成 \r\n —— 结果 JSON 就不再
     * 逐字节可比（Linux/macOS 上这个调用是空操作）。 */
    wbc_stdout_binary();
    memset(&opts, 0, sizeof(opts));
    memset(&stats, 0, sizeof(stats));
    opts.default_realm = "intl";
    opts.realm = "all";
    opts.series_realm = "all";
    opts.perf_realm = "all";
    opts.since = 0.0;
    opts.until = 1e18;
    opts.step = 3600;
    opts.perf_sample = 5000;
    opts.stats = &stats;

    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;
        if (!strcmp(a, "--log") && v) opts.log = v;
        else if (!strcmp(a, "--policies") && v) opts.policies = v;
        else if (!strcmp(a, "--timeline") && v) opts.timeline = v;
        else if (!strcmp(a, "--pricing") && v) opts.pricing = v;
        else if (!strcmp(a, "--accounts") && v) opts.accounts = v;
        else if (!strcmp(a, "--default-realm") && v) opts.default_realm = v;
        else if (!strcmp(a, "--realm") && v) opts.realm = v;
        else if (!strcmp(a, "--series-realm") && v) opts.series_realm = v;
        else if (!strcmp(a, "--out") && v) out_path = v;
        else if (!strcmp(a, "--only") && v) only = v;
        else if (!strcmp(a, "--since") && v) { opts.since = atof(v); opts.has_since = 1; }
        else if (!strcmp(a, "--until") && v) { opts.until = atof(v); opts.has_until = 1; }
        else if (!strcmp(a, "--step") && v) opts.step = atoll(v);
        else if (!strcmp(a, "--perf-sample") && v) opts.perf_sample = atoll(v);
        else if (!strcmp(a, "--perf-realm") && v) opts.perf_realm = v;
        else if (!strcmp(a, "--events") && v) opts.events = v;
        else if (!strcmp(a, "--estimate-daily") && v) opts.estimate_daily = v;
        else if (!strcmp(a, "--now") && v) {
            opts.now_set = 1;
            opts.now = atof(v);
        }
        else if (!strcmp(a, "--daily-midnight") && v) {
            opts.daily_set = 1;
            opts.daily_midnight = atof(v);
        } else {
            usage();
            return 2;
        }
        i++;
    }
    if (!opts.log) {
        usage();
        return 2;
    }
    if (only) {
        if (strstr(only, "snapshot")) opts.sections |= WBC_SEC_SNAPSHOT;
        if (strstr(only, "analytics")) opts.sections |= WBC_SEC_ANALYTICS;
        if (strstr(only, "series")) opts.sections |= WBC_SEC_SERIES;
        if (strstr(only, "by_account")) opts.sections |= WBC_SEC_BY_ACCOUNT;
        if (strstr(only, "count")) opts.sections |= WBC_SEC_COUNT;
        if (strstr(only, "daily")) opts.sections |= WBC_SEC_DAILY;
        if (strstr(only, "perf")) opts.sections |= WBC_SEC_PERF;
        if (strstr(only, "remaining")) opts.sections |= WBC_SEC_REMAINING;
    } else {
        opts.sections = WBC_SEC_ALL;
    }

    buf_init(&out);
    rc = engine_run(&opts, &out);
    if (rc != 0) {
        if (rc == 1) fprintf(stderr, "wb-agg: cannot read %s\n", opts.log);
        else fprintf(stderr, "wb-agg: out of memory\n");
        return rc;
    }
    if (out_path) {
        FILE *fh = fopen(out_path, "wb");
        if (!fh) {
            fprintf(stderr, "wb-agg: cannot write %s\n", out_path);
            return 1;
        }
        fwrite(out.data, 1, out.len, fh);
        fclose(fh);
    } else {
        fwrite(out.data, 1, out.len, stdout);
    }
    fprintf(stderr, "wb-agg: %zu lines, %zu rows, %zu bad, %.3fs (load %.3f, parse %.3f, fold %.3f)\n",
            stats.n_lines, stats.n_rows, stats.n_bad, stats.seconds,
            stats.load_s, stats.parse_s, stats.fold_s);
    buf_free(&out);
    return 0;
}
