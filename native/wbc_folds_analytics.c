/* Analytics / series / by-account folds for the standalone aggregation engine.
 *
 * Ported field for field from wb_proxy.py (main e6b98fdd):
 *   _fold_usage_analytics + _feed_analytics + _bump_analytics_models
 *   _usage_timeseries_scan (the cold scan)
 *   _fold_usage_by_account
 *
 * Numeric policy (CONTRACT.md): every token field runs through Num (exact
 * int64 while all contributions are integers, float once one is not); credit,
 * cost, ttft, speed and elapsed accumulate as plain doubles in log order;
 * counters are int64.  Output goes through the caller's JW; the parity
 * harness compares with dict semantics, so map order does not matter (the
 * series bucket list is a list and is emitted in key order).
 *
 * TIMING METRICS: _feed_analytics folds ttft / speed / elapsed into a stat
 * only when the row's raw value is truthy (`if vals[6]:`), bumping the
 * matching *_n counter in the same branch.  The standalone fold only receives
 * `const Row *`, so Row must expose those three raw numbers (wbc_row.h
 * doubles read from "ttft_ms", "tokens_per_sec" and "elapsed_ms", falsy ->
 * 0.0, the same read_num() shape as the other row numbers).  The three macros
 * below are the single place that names them: point them at the right fields
 * (or at 0.0, which reports the metrics as unmeasured) if the header uses
 * different names.
 */

#include "wbc_folds.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define ROW_TTFT(r)    ((r)->ttft)
#define ROW_SPEED(r)   ((r)->speed)
#define ROW_ELAPSED(r) ((r)->elapsed)

/* ---- small shared helpers ---------------------------------------------- */

/* Same failure policy as wbc_util.c: the engine cannot continue without
 * memory, so report and leave. */
static void *fa_xmalloc(size_t n)
{
    void *p = malloc(n);
    if (!p) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    return p;
}

static void *fa_xcalloc(size_t n, size_t size)
{
    void *p = calloc(n, size);
    if (!p) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    return p;
}

static void *fa_xrealloc(void *p, size_t n)
{
    void *q = realloc(p, n);
    if (!q) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    return q;
}

/* Python `value or fallback` for the strings a row can carry: missing or
 * empty takes the fallback. */
static const char *fa_str_or(const char *s, const char *fallback)
{
    return (s && s[0]) ? s : fallback;
}

/* `row.get(field) or ""` / `row.get(field, "")`: NULL reads as empty. */
static const char *fa_str_or_empty(const char *s)
{
    return s ? s : "";
}

/* A Num as JSON: exact int64 while no non-integer contribution appeared. */
static void fa_jw_num(JW *out, const Num *n)
{
    if (n->has_float) jw_double(out, n->fsum);
    else jw_int(out, (long long)n->isum);
}

/* A raw row number that kept its int/float shape (credit entry total_tokens,
 * the only place a row value is written out again unchanged). */
static void fa_jw_row_num(JW *out, double value, int is_int)
{
    if (is_int) jw_int(out, (long long)value);
    else jw_double(out, value);
}

/* map_free releases the keys; every value stored by these folds is heap-owned
 * here, so drop the values first. */
static void fa_map_free_values(Map *m)
{
    size_t i;
    for (i = 0; i < m->cap; i++) free(m->ents[i].val);
    map_free(m);
}

/* km["realms"][key] = km["realms"].get(key, 0) + 1 (int64 counts). */
static void fa_count_bump(Map *m, const char *key)
{
    int64_t *slot = (int64_t *)map_get(m, key);
    if (slot) {
        *slot += 1;
        return;
    }
    slot = (int64_t *)fa_xmalloc(sizeof(*slot));
    *slot = 1;
    map_put(m, key, slot);
}

/* ======================================================================== */
/* analytics: the all-time half only (CONTRACT.md scope)                     */
/* ======================================================================== */

/* One analytics stat: the fifteen accumulators of _new_analytics_stat. */
typedef struct {
    int64_t requests, errors;
    Num prompt, completion, reasoning, cached, total;
    double credit, cost_cny;
    double ttft_sum;
    int64_t ttft_n;
    double speed_sum;
    int64_t speed_n;
    double elapsed_sum;
    int64_t elapsed_n;
} AStat;

/* One bucket of an all_models map: {"requests","tokens","reasoning",
 * "cost_cny"} — successful requests only. */
typedef struct {
    int64_t requests;
    Num tokens;
    Num reasoning;
    double cost_cny;
} AModelBucket;

typedef struct {
    const char *uid;
    const char *realm;          /* the row's realm field; first writer wins */
    AStat all_time;
    Map all_models;             /* model id -> AModelBucket* */
} AAcct;

typedef struct {
    const char *model;
    AStat all_time;
} AModel;

typedef struct {
    const char *key;
    AStat all_time;
    Map all_models;             /* model id -> AModelBucket* */
    Map realms;                 /* realm -> int64* */
    double last_at;
} AKey;

/* The four maps one half folds into; the all-time half owns `all_time` and
 * `all_models` on every entry. */
typedef struct {
    AStat summary;
    Map accts;                  /* uid -> AAcct* */
    Map models;                 /* model id -> AModel* */
    Map keys;                   /* key id -> AKey* */
} AMaps;

/* _feed_analytics: the counters depend on the outcome, the token/credit
 * totals do not (a failed request still consumed what the upstream billed).
 * The three timing metrics only count when the row recorded them — a falsy
 * raw value (0 / 0.0 / absent) is skipped and does not bump the counter. */
static void fa_analytics_feed(AStat *st, const Row *r, int is_err, double cost_cny)
{
    double metric;
    if (is_err) st->errors += 1;
    else st->requests += 1;
    num_add(&st->prompt, r->prompt, r->prompt_i);
    num_add(&st->completion, r->completion, r->completion_i);
    num_add(&st->reasoning, r->reasoning, r->reasoning_i);
    num_add(&st->cached, r->cached, r->cached_i);
    num_add(&st->total, r->total, r->total_i);
    st->credit += r->credit;
    st->cost_cny += cost_cny;
    metric = ROW_TTFT(r);
    if (metric != 0.0) {
        st->ttft_sum += metric;
        st->ttft_n += 1;
    }
    metric = ROW_SPEED(r);
    if (metric != 0.0) {
        st->speed_sum += metric;
        st->speed_n += 1;
    }
    metric = ROW_ELAPSED(r);
    if (metric != 0.0) {
        st->elapsed_sum += metric;
        st->elapsed_n += 1;
    }
}

/* _bump_analytics_models: successful requests only, and a failed row creates
 * no bucket at all (a failed call attributed to a model would read as demand
 * for it). */
static void fa_analytics_bump_models(Map *tgt, const char *m_id, const Row *r,
                                     int is_err, double cost_cny)
{
    AModelBucket *b;
    if (is_err) return;
    b = (AModelBucket *)map_get(tgt, m_id);
    if (!b) {
        b = (AModelBucket *)fa_xcalloc(1, sizeof(*b));
        map_put(tgt, m_id, b);
    }
    b->requests += 1;
    num_add(&b->tokens, r->total, r->total_i);
    num_add(&b->reasoning, r->reasoning, r->reasoning_i);
    b->cost_cny += cost_cny;
}

/* _row_cost(...)["cny"] when the price is known, else 0.0.  The standalone
 * engine has no price-master switch (CONTRACT.md: Cost.disabled stays 0). */
static double fa_analytics_cost(const Pricing *pr, const Row *r)
{
    Cost cost;
    if (!pr) return 0.0;
    memset(&cost, 0, sizeof(cost));
    pricing_cost(pr, r->model, r->has_at ? r->at : 0.0,
                 r->prompt, r->cached, r->completion, r->cost_policy, &cost);
    return cost.known ? cost.cny : 0.0;
}

/* _fold_usage_analytics, one row into one half.  The all-time fold has no
 * realm filter (the reference's default realm for the analytics payload is
 * None = every realm), and a client cancellation is dropped whole — before
 * pricing, before field extraction, before any bucket exists. */
static void fa_analytics_row(AMaps *m, const Row *r, const Pricing *pr,
                             const Map *accounts, const char *default_realm)
{
    const char *outcome = row_outcome_of(r);
    const char *acct_uid, *m_id, *k_id, *k_realm;
    double cost_cny, at;
    int is_err;
    AAcct *acct;
    AModel *model;
    AKey *key;

    if (!strcmp(outcome, "client_aborted")) return;
    is_err = strcmp(outcome, "completed") != 0;
    cost_cny = fa_analytics_cost(pr, r);
    acct_uid = fa_str_or(r->account, "(unattributed)");
    m_id = fa_str_or(r->model, "(unknown)");
    /* No "key" field at all -> the pre-feature bucket; a present but empty
     * key -> the anonymous bucket.  The two never merge. */
    k_id = r->has_key ? fa_str_or(r->key, "__no_key__") : "__before_keys__";
    k_realm = fa_str_or_empty(row_realm_of(r, accounts, default_realm));
    at = r->has_at ? r->at : 0.0;

    fa_analytics_feed(&m->summary, r, is_err, cost_cny);

    acct = (AAcct *)map_get(&m->accts, acct_uid);
    if (!acct) {
        acct = (AAcct *)fa_xcalloc(1, sizeof(*acct));
        acct->uid = acct_uid;
        acct->realm = fa_str_or_empty(r->realm);   /* field, not row_realm() */
        map_init(&acct->all_models);
        map_put(&m->accts, acct_uid, acct);
    }
    fa_analytics_feed(&acct->all_time, r, is_err, cost_cny);
    fa_analytics_bump_models(&acct->all_models, m_id, r, is_err, cost_cny);

    model = (AModel *)map_get(&m->models, m_id);
    if (!model) {
        model = (AModel *)fa_xcalloc(1, sizeof(*model));
        model->model = m_id;
        map_put(&m->models, m_id, model);
    }
    fa_analytics_feed(&model->all_time, r, is_err, cost_cny);

    key = (AKey *)map_get(&m->keys, k_id);
    if (!key) {
        key = (AKey *)fa_xcalloc(1, sizeof(*key));
        key->key = k_id;
        map_init(&key->all_models);
        map_init(&key->realms);
        map_put(&m->keys, k_id, key);
    }
    fa_count_bump(&key->realms, k_realm);
    if (at != 0.0 && at > key->last_at) key->last_at = at;
    fa_analytics_feed(&key->all_time, r, is_err, cost_cny);
    fa_analytics_bump_models(&key->all_models, m_id, r, is_err, cost_cny);
}

/* The stat shape, in _new_analytics_stat() order. */
static void fa_jw_stat(JW *out, const AStat *st)
{
    jw_obj_begin(out);
    jw_key(out, "requests");
    jw_int(out, (long long)st->requests);
    jw_key(out, "errors");
    jw_int(out, (long long)st->errors);
    jw_key(out, "prompt_tokens");
    fa_jw_num(out, &st->prompt);
    jw_key(out, "completion_tokens");
    fa_jw_num(out, &st->completion);
    jw_key(out, "reasoning_tokens");
    fa_jw_num(out, &st->reasoning);
    jw_key(out, "cached_tokens");
    fa_jw_num(out, &st->cached);
    jw_key(out, "total_tokens");
    fa_jw_num(out, &st->total);
    jw_key(out, "credit");
    jw_double(out, st->credit);
    jw_key(out, "cost_cny");
    jw_double(out, st->cost_cny);
    jw_key(out, "ttft_sum");
    jw_double(out, st->ttft_sum);
    jw_key(out, "ttft_n");
    jw_int(out, (long long)st->ttft_n);
    jw_key(out, "speed_sum");
    jw_double(out, st->speed_sum);
    jw_key(out, "speed_n");
    jw_int(out, (long long)st->speed_n);
    jw_key(out, "elapsed_sum");
    jw_double(out, st->elapsed_sum);
    jw_key(out, "elapsed_n");
    jw_int(out, (long long)st->elapsed_n);
    jw_obj_end(out);
}

static void fa_jw_model_buckets(JW *out, const Map *buckets)
{
    size_t i;
    jw_obj_begin(out);
    for (i = 0; i < buckets->cap; i++) {
        const AModelBucket *b;
        if (!buckets->ents[i].key) continue;
        b = (const AModelBucket *)buckets->ents[i].val;
        jw_key(out, buckets->ents[i].key);
        jw_obj_begin(out);
        jw_key(out, "requests");
        jw_int(out, (long long)b->requests);
        jw_key(out, "tokens");
        fa_jw_num(out, &b->tokens);
        jw_key(out, "reasoning");
        fa_jw_num(out, &b->reasoning);
        jw_key(out, "cost_cny");
        jw_double(out, b->cost_cny);
        jw_obj_end(out);
    }
    jw_obj_end(out);
}

static void fa_free_analytics_maps(AMaps *m)
{
    size_t i;
    for (i = 0; i < m->accts.cap; i++) {
        AAcct *a = (AAcct *)m->accts.ents[i].val;
        if (!a) continue;
        fa_map_free_values(&a->all_models);
        free(a);
    }
    map_free(&m->accts);
    fa_map_free_values(&m->models);
    for (i = 0; i < m->keys.cap; i++) {
        AKey *k = (AKey *)m->keys.ents[i].val;
        if (!k) continue;
        fa_map_free_values(&k->all_models);
        fa_map_free_values(&k->realms);
        free(k);
    }
    map_free(&m->keys);
}

void fold_analytics(const Row *rows, size_t n, const Pricing *pr,
                    const Map *accounts, const char *default_realm, JW *out)
{
    AMaps maps;
    size_t i;

    memset(&maps, 0, sizeof(maps));
    map_init(&maps.accts);
    map_init(&maps.models);
    map_init(&maps.keys);
    for (i = 0; i < n; i++)
        fa_analytics_row(&maps, &rows[i], pr, accounts, default_realm);

    jw_obj_begin(out);
    jw_key(out, "summary");
    fa_jw_stat(out, &maps.summary);

    jw_key(out, "accts");
    jw_obj_begin(out);
    for (i = 0; i < maps.accts.cap; i++) {
        const AAcct *a = (const AAcct *)maps.accts.ents[i].val;
        if (!a) continue;
        jw_key(out, maps.accts.ents[i].key);
        jw_obj_begin(out);
        jw_key(out, "uid");
        jw_str(out, a->uid);
        jw_key(out, "nickname");
        jw_str(out, a->uid);
        jw_key(out, "realm");
        jw_str(out, a->realm);
        jw_key(out, "domain");
        jw_str(out, "");
        jw_key(out, "all_time");
        fa_jw_stat(out, &a->all_time);
        jw_key(out, "all_models");
        fa_jw_model_buckets(out, &a->all_models);
        jw_obj_end(out);
    }
    jw_obj_end(out);

    jw_key(out, "models");
    jw_obj_begin(out);
    for (i = 0; i < maps.models.cap; i++) {
        const AModel *mo = (const AModel *)maps.models.ents[i].val;
        if (!mo) continue;
        jw_key(out, maps.models.ents[i].key);
        jw_obj_begin(out);
        jw_key(out, "model");
        jw_str(out, mo->model);
        jw_key(out, "all_time");
        fa_jw_stat(out, &mo->all_time);
        jw_obj_end(out);
    }
    jw_obj_end(out);

    jw_key(out, "keys");
    jw_obj_begin(out);
    for (i = 0; i < maps.keys.cap; i++) {
        const AKey *k = (const AKey *)maps.keys.ents[i].val;
        size_t j;
        if (!k) continue;
        jw_key(out, maps.keys.ents[i].key);
        jw_obj_begin(out);
        jw_key(out, "key");
        jw_str(out, k->key);
        jw_key(out, "all_time");
        fa_jw_stat(out, &k->all_time);
        jw_key(out, "all_models");
        fa_jw_model_buckets(out, &k->all_models);
        jw_key(out, "realms");
        jw_obj_begin(out);
        for (j = 0; j < k->realms.cap; j++) {
            if (!k->realms.ents[j].key) continue;
            jw_key(out, k->realms.ents[j].key);
            jw_int(out, (long long)*(const int64_t *)k->realms.ents[j].val);
        }
        jw_obj_end(out);
        jw_key(out, "last_at");
        jw_double(out, k->last_at);
        jw_obj_end(out);
    }
    jw_obj_end(out);
    jw_obj_end(out);

    fa_free_analytics_maps(&maps);
}

/* ======================================================================== */
/* series: the cold scan (_usage_timeseries_scan)                            */
/* ======================================================================== */

typedef struct {
    long long key;
    double at;                  /* lo + key * step */
    int64_t requests, errors;
    Num prompt, completion, reasoning, cached, total;
    double credit;
} SeriesBucket;

typedef struct {
    double at;
    size_t seq;                 /* collection order == log order */
    const char *iso;
    const char *model;
    const char *account;
    double credit;
    double total;
    int total_i;
} SeriesCredit;

/* Buckets are emitted in key order (the reference sorts the keys). */
static int fa_bucket_cmp(const void *pa, const void *pb)
{
    const SeriesBucket *a = *(SeriesBucket *const *)pa;
    const SeriesBucket *b = *(SeriesBucket *const *)pb;
    if (a->key < b->key) return -1;
    if (a->key > b->key) return 1;
    return 0;
}

/* credits: at descending, log order ascending for equal at — the reference
 * keeps the 50 largest (at, -index) tuples and sorts them the same way. */
static int fa_credit_cmp(const void *pa, const void *pb)
{
    const SeriesCredit *a = (const SeriesCredit *)pa;
    const SeriesCredit *b = (const SeriesCredit *)pb;
    if (a->at > b->at) return -1;
    if (a->at < b->at) return 1;
    if (a->seq < b->seq) return -1;
    if (a->seq > b->seq) return 1;
    return 0;
}

void fold_series(const Row *rows, size_t n, double lo, double hi, long long step,
                 const char *realm, const Map *accounts,
                 const char *default_realm, JW *out)
{
    Map buckets;
    SeriesBucket **order = NULL;
    size_t order_len = 0, order_cap = 0;
    SeriesCredit *credits = NULL;
    size_t credits_len = 0, credits_cap = 0;
    size_t i;

    map_init(&buckets);
    /* step <= 0 is a caller bug (the reference would raise); emit an empty
     * scan rather than divide by zero. */
    if (step > 0) {
        for (i = 0; i < n; i++) {
            const Row *r = &rows[i];
            const char *outcome;
            double at, credit;
            long long key;
            char kb[32];
            SeriesBucket *b;

            /* NULL / empty / "all" = every realm. */
            if (realm && realm[0] && strcmp(realm, "all") != 0) {
                const char *rr = row_realm_of(r, accounts, default_realm);
                if (!rr || strcmp(rr, realm) != 0) continue;
            }
            at = r->has_at ? r->at : 0.0;
            if (at < lo || at > hi) continue;   /* both ends inclusive */
            /* at >= lo here, so the floor division is a plain truncation. */
            key = (long long)((at - lo) / (double)step);
            snprintf(kb, sizeof(kb), "%lld", key);
            b = (SeriesBucket *)map_get(&buckets, kb);
            if (!b) {
                b = (SeriesBucket *)fa_xcalloc(1, sizeof(*b));
                b->key = key;
                b->at = lo + (double)key * (double)step;
                map_put(&buckets, kb, b);
                if (order_len == order_cap) {
                    order_cap = order_cap ? order_cap * 2 : 64;
                    order = (SeriesBucket **)fa_xrealloc(order,
                                                         order_cap * sizeof(*order));
                }
                order[order_len++] = b;
            }
            outcome = row_outcome_of(r);
            if (!strcmp(outcome, "completed")) {
                b->requests += 1;
                num_add(&b->prompt, r->prompt, r->prompt_i);
                num_add(&b->completion, r->completion, r->completion_i);
                num_add(&b->reasoning, r->reasoning, r->reasoning_i);
                num_add(&b->cached, r->cached, r->cached_i);
                num_add(&b->total, r->total, r->total_i);
            } else {
                b->errors += 1;
            }
            /* credit rides its own rule: everything except a client abort
             * feeds the bucket, and a positive credit is a report row even
             * when the outcome was an abort. */
            credit = r->credit;
            if (strcmp(outcome, "client_aborted") != 0) b->credit += credit;
            if (credit > 0.0) {
                if (credits_len == credits_cap) {
                    credits_cap = credits_cap ? credits_cap * 2 : 64;
                    credits = (SeriesCredit *)fa_xrealloc(
                        credits, credits_cap * sizeof(*credits));
                }
                credits[credits_len].at = at;
                credits[credits_len].seq = credits_len;
                credits[credits_len].iso = fa_str_or_empty(r->iso);
                credits[credits_len].model = fa_str_or_empty(r->model);
                credits[credits_len].account = fa_str_or_empty(r->account);
                credits[credits_len].credit = credit;
                credits[credits_len].total = r->total;
                credits[credits_len].total_i = r->total_i;
                credits_len++;
            }
        }
    }
    if (order_len > 1) qsort(order, order_len, sizeof(*order), fa_bucket_cmp);
    if (credits_len > 1) qsort(credits, credits_len, sizeof(*credits), fa_credit_cmp);
    if (credits_len > 50) credits_len = 50;

    jw_obj_begin(out);
    jw_key(out, "bucket_seconds");
    jw_int(out, step);
    jw_key(out, "since");
    jw_double(out, lo);
    jw_key(out, "until");
    jw_double(out, hi);
    jw_key(out, "series");
    jw_arr_begin(out);
    for (i = 0; i < order_len; i++) {
        const SeriesBucket *b = order[i];
        jw_obj_begin(out);
        jw_key(out, "at");
        jw_double(out, b->at);
        jw_key(out, "requests");
        jw_int(out, (long long)b->requests);
        jw_key(out, "errors");
        jw_int(out, (long long)b->errors);
        jw_key(out, "prompt_tokens");
        fa_jw_num(out, &b->prompt);
        jw_key(out, "completion_tokens");
        fa_jw_num(out, &b->completion);
        jw_key(out, "reasoning_tokens");
        fa_jw_num(out, &b->reasoning);
        jw_key(out, "cached_tokens");
        fa_jw_num(out, &b->cached);
        jw_key(out, "total_tokens");
        fa_jw_num(out, &b->total);
        jw_key(out, "credit");
        jw_double(out, b->credit);
        jw_obj_end(out);
    }
    jw_arr_end(out);
    jw_key(out, "credits");
    jw_arr_begin(out);
    for (i = 0; i < credits_len; i++) {
        const SeriesCredit *c = &credits[i];
        jw_obj_begin(out);
        jw_key(out, "at");
        jw_double(out, c->at);
        jw_key(out, "iso");
        jw_str(out, c->iso);
        jw_key(out, "model");
        jw_str(out, c->model);
        jw_key(out, "account");
        jw_str(out, c->account);
        jw_key(out, "credit");
        jw_double(out, c->credit);
        jw_key(out, "total_tokens");
        fa_jw_row_num(out, c->total, c->total_i);
        jw_obj_end(out);
    }
    jw_arr_end(out);
    jw_obj_end(out);

    fa_map_free_values(&buckets);
    free(order);
    free(credits);
}

/* ======================================================================== */
/* by-account (_fold_usage_by_account)                                       */
/* ======================================================================== */

typedef struct {
    const char *account;
    int64_t requests;
    Num prompt, completion, reasoning, cached, total;
    Map models;                 /* model -> int64* */
} ByAcctBucket;

void fold_by_account(const Row *rows, size_t n, JW *out)
{
    Map buckets;
    size_t i;

    map_init(&buckets);
    for (i = 0; i < n; i++) {
        const Row *r = &rows[i];
        const char *key;
        ByAcctBucket *b;
        /* This view has always described the whole log: no realm filter, no
         * window, and only the error flag skips a row (not the outcome). */
        if (r->error_truthy) continue;
        key = fa_str_or(r->account, "(unattributed)");
        b = (ByAcctBucket *)map_get(&buckets, key);
        if (!b) {
            b = (ByAcctBucket *)fa_xcalloc(1, sizeof(*b));
            b->account = key;
            map_init(&b->models);
            map_put(&buckets, key, b);
        }
        b->requests += 1;
        num_add(&b->prompt, r->prompt, r->prompt_i);
        num_add(&b->completion, r->completion, r->completion_i);
        num_add(&b->reasoning, r->reasoning, r->reasoning_i);
        num_add(&b->cached, r->cached, r->cached_i);
        num_add(&b->total, r->total, r->total_i);
        fa_count_bump(&b->models, fa_str_or(r->model, "?"));
    }

    jw_obj_begin(out);
    for (i = 0; i < buckets.cap; i++) {
        const ByAcctBucket *b = (const ByAcctBucket *)buckets.ents[i].val;
        size_t j;
        if (!b) continue;
        jw_key(out, buckets.ents[i].key);
        jw_obj_begin(out);
        jw_key(out, "account");
        jw_str(out, b->account);
        jw_key(out, "requests");
        jw_int(out, (long long)b->requests);
        jw_key(out, "prompt_tokens");
        fa_jw_num(out, &b->prompt);
        jw_key(out, "completion_tokens");
        fa_jw_num(out, &b->completion);
        jw_key(out, "reasoning_tokens");
        fa_jw_num(out, &b->reasoning);
        jw_key(out, "cached_tokens");
        fa_jw_num(out, &b->cached);
        jw_key(out, "total_tokens");
        fa_jw_num(out, &b->total);
        jw_key(out, "models");
        jw_obj_begin(out);
        for (j = 0; j < b->models.cap; j++) {
            if (!b->models.ents[j].key) continue;
            jw_key(out, b->models.ents[j].key);
            jw_int(out, (long long)*(const int64_t *)b->models.ents[j].val);
        }
        jw_obj_end(out);
        jw_obj_end(out);
    }
    jw_obj_end(out);

    for (i = 0; i < buckets.cap; i++) {
        ByAcctBucket *b = (ByAcctBucket *)buckets.ents[i].val;
        if (!b) continue;
        fa_map_free_values(&b->models);
        free(b);
    }
    map_free(&buckets);
}
