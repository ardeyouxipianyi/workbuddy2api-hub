/* Snapshot / count / daily folds for the standalone wb-agg engine.
 *
 * Verbatim port of the reference folds in native-full/wb_proxy.py:
 *   _fold_usage_snapshot + _fold_cost   -> fold_snapshot
 *   _count_usage_line                   -> fold_count
 *   _scan_daily_usage                   -> fold_daily
 *
 * The structures below are private to this file; only the exported folds in
 * wbc_folds.h are shared with main.c / the analytics half.  Numeric policy
 * matches the Python folds: token sums go through Num (exact int64 while
 * every contribution is an integer, float once one is not), credit / cost_cny
 * accumulate as plain doubles in log order, counters are int64.
 */

#include "wbc_folds.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------------ */
/* snapshot bucket                                                          */
/* ------------------------------------------------------------------------ */

/* One snapshot bucket.  The totals bucket fills every map; the by_model /
 * by_model_realm / by_model_acct buckets only use requests, the token Nums,
 * credit, cost_cny and accounts (that is exactly the key set Python builds
 * with setdefault - no errors, no cost_missing on the nested ones). */
typedef struct SBucket {
    int64_t requests, errors;
    Num prompt, completion, reasoning, cached, total;
    double credit, cost_cny;
    Map cost_missing;    /* model -> int64*  (unpriced row count)          */
    Map accounts;        /* acct  -> int64*  (completed rows per account)  */
    Map by_model;        /* model -> SBucket*                              */
    Map by_model_realm;  /* model -> Map* (realm -> SBucket*)              */
    Map by_model_acct;   /* model -> Map* (realm -> Map* (acct -> SBucket*)) */
} SBucket;

/* ------------------------------------------------------------------------ */
/* small helpers                                                            */
/* ------------------------------------------------------------------------ */

static void oom(void)
{
    fprintf(stderr, "wb-agg: out of memory\n");
    exit(3);
}

static void *xmalloc(size_t n)
{
    void *p = malloc(n);
    if (!p) oom();
    return p;
}

static void *xcalloc(size_t n, size_t size)
{
    void *p = calloc(n, size);
    if (!p) oom();
    return p;
}

/* int64 counter living in a Map: missing.get(key, 0) + 1. */
static void count_inc(Map *m, const char *key)
{
    int64_t *cnt = (int64_t *)map_get(m, key);
    if (cnt) {
        *cnt += 1;
        return;
    }
    cnt = (int64_t *)xmalloc(sizeof *cnt);
    *cnt = 1;
    map_put(m, key, cnt);
}

/* Same, for a key that is a slice of a line and not NUL terminated. */
static void count_inc_slice(Map *m, const char *key, size_t len)
{
    char *copy = (char *)xmalloc(len + 1);
    memcpy(copy, key, len);
    copy[len] = '\0';
    count_inc(m, copy);
    free(copy);
}

static void count_map_free(Map *m)
{
    size_t i;
    for (i = 0; i < m->cap; i++) free(m->ents[i].val);
    map_free(m);
}

static SBucket *bucket_new(void)
{
    SBucket *b = (SBucket *)xcalloc(1, sizeof *b);
    map_init(&b->cost_missing);
    map_init(&b->accounts);
    map_init(&b->by_model);
    map_init(&b->by_model_realm);
    map_init(&b->by_model_acct);
    return b;
}

static Map *map_new(void)
{
    Map *m = (Map *)xmalloc(sizeof *m);
    map_init(m);
    return m;
}

static Map *map_child_map(Map *m, const char *key)
{
    Map *found = (Map *)map_get(m, key);
    if (found) return found;
    found = map_new();
    map_put(m, key, found);
    return found;
}

static SBucket *bucket_child(Map *m, const char *key)
{
    SBucket *found = (SBucket *)map_get(m, key);
    if (found) return found;
    found = bucket_new();
    map_put(m, key, found);
    return found;
}

/* Recursive release of a bucket and everything it owns.  Row data belongs to
 * the caller and is never touched here. */
static void bucket_free(SBucket *b)
{
    size_t i, j, k;
    if (!b) return;
    count_map_free(&b->cost_missing);
    count_map_free(&b->accounts);
    for (i = 0; i < b->by_model.cap; i++) {
        if (!b->by_model.ents[i].key) continue;
        bucket_free((SBucket *)b->by_model.ents[i].val);
    }
    map_free(&b->by_model);
    for (i = 0; i < b->by_model_realm.cap; i++) {
        Map *rm;
        if (!b->by_model_realm.ents[i].key) continue;
        rm = (Map *)b->by_model_realm.ents[i].val;
        for (j = 0; j < rm->cap; j++) {
            if (!rm->ents[j].key) continue;
            bucket_free((SBucket *)rm->ents[j].val);
        }
        map_free(rm);
        free(rm);
    }
    map_free(&b->by_model_realm);
    for (i = 0; i < b->by_model_acct.cap; i++) {
        Map *rm;
        if (!b->by_model_acct.ents[i].key) continue;
        rm = (Map *)b->by_model_acct.ents[i].val;
        for (j = 0; j < rm->cap; j++) {
            Map *am;
            if (!rm->ents[j].key) continue;
            am = (Map *)rm->ents[j].val;
            for (k = 0; k < am->cap; k++) {
                if (!am->ents[k].key) continue;
                bucket_free((SBucket *)am->ents[k].val);
            }
            map_free(am);
            free(am);
        }
        map_free(rm);
        free(rm);
    }
    map_free(&b->by_model_acct);
    free(b);
}

/* model or "unknown" (Python `or` truthiness: empty string is "unknown"). */
static const char *model_key(const char *model)
{
    return (model && model[0]) ? model : "unknown";
}

/* Realm filter contract of the C API: NULL / "" / "all" mean every realm
 * (row_matches_realm accepts the literal "all" too); any other value is the
 * realm rows must match, exactly like _fold_usage_snapshot's `r`. */
static int realm_filter_active(const char *realm)
{
    return realm && realm[0] && strcmp(realm, "all") != 0;
}

/* Fallback for rows with no realm of their own: the filter value doubles as
 * the default realm here (main.c passes --default-realm, default "intl",
 * which is also wb_proxy.CURRENT_REALM); "all" is a scope word, not a realm,
 * so it falls back to "intl" like the Python module default. */
static const char *realm_fallback(const char *realm)
{
    return realm_filter_active(realm) ? realm : "intl";
}

/* wb_proxy._fold_cost: known -> cost_cny += cny; otherwise (and not
 * disabled) the model is counted in cost_missing.  A row the master switch
 * left unpriced is not "missing a price" - the standalone engine never sets
 * disabled, but the branch stays for shape parity. */
static void fold_cost(SBucket *b, const Cost *cost, const char *model)
{
    if (cost->known) {
        b->cost_cny += cost->cny;
    } else if (!cost->disabled) {
        count_inc(&b->cost_missing, model_key(model));
    }
}

static Num *num_child(Map *m, const char *key)
{
    Num *found = (Num *)map_get(m, key);
    if (found) return found;
    found = (Num *)xmalloc(sizeof *found);
    found->isum = 0;
    found->fsum = 0.0;
    found->has_float = 0;
    map_put(m, key, found);
    return found;
}

static double *double_child(Map *m, const char *key)
{
    double *found = (double *)map_get(m, key);
    if (found) return found;
    found = (double *)xmalloc(sizeof *found);
    *found = 0.0;
    map_put(m, key, found);
    return found;
}

/* ------------------------------------------------------------------------ */
/* snapshot serialization                                                   */
/* ------------------------------------------------------------------------ */

static void jw_num_field(JW *out, const char *key, const Num *n)
{
    jw_key(out, key);
    if (n->has_float) jw_double(out, num_value(n));
    else jw_int(out, (long long)n->isum);
}

static void jw_num_value(JW *out, const Num *n)
{
    if (n->has_float) jw_double(out, num_value(n));
    else jw_int(out, (long long)n->isum);
}

static void jw_count_map(JW *out, const char *key, const Map *m)
{
    size_t i;
    jw_key(out, key);
    jw_obj_begin(out);
    for (i = 0; i < m->cap; i++) {
        if (!m->ents[i].key || !m->ents[i].val) continue;
        jw_key(out, m->ents[i].key);
        jw_int(out, (long long)*(const int64_t *)m->ents[i].val);
    }
    jw_obj_end(out);
}

/* The nested bucket shape Python creates with
 * {"requests": 0, "accounts": {}, "cost_cny": 0.0, **{k: 0 for k in USAGE_FIELDS}}. */
static void bucket_write_nested(JW *out, const SBucket *b)
{
    jw_obj_begin(out);
    jw_key(out, "requests");
    jw_int(out, (long long)b->requests);
    jw_count_map(out, "accounts", &b->accounts);
    jw_key(out, "cost_cny");
    jw_double(out, b->cost_cny);
    jw_num_field(out, "prompt_tokens", &b->prompt);
    jw_num_field(out, "completion_tokens", &b->completion);
    jw_num_field(out, "reasoning_tokens", &b->reasoning);
    jw_num_field(out, "cached_tokens", &b->cached);
    jw_num_field(out, "total_tokens", &b->total);
    jw_key(out, "credit");
    jw_double(out, b->credit);
    jw_obj_end(out);
}

/* The snapshot object itself (main.c writes the "snapshot" key first). */
static void snapshot_write(JW *out, const SBucket *b)
{
    size_t i, j, k;
    jw_obj_begin(out);
    jw_key(out, "requests");
    jw_int(out, (long long)b->requests);
    jw_key(out, "errors");
    jw_int(out, (long long)b->errors);
    jw_num_field(out, "prompt_tokens", &b->prompt);
    jw_num_field(out, "completion_tokens", &b->completion);
    jw_num_field(out, "reasoning_tokens", &b->reasoning);
    jw_num_field(out, "cached_tokens", &b->cached);
    jw_num_field(out, "total_tokens", &b->total);
    jw_key(out, "credit");
    jw_double(out, b->credit);
    jw_key(out, "cost_cny");
    jw_double(out, b->cost_cny);
    jw_count_map(out, "cost_missing", &b->cost_missing);

    jw_key(out, "by_model");
    jw_obj_begin(out);
    for (i = 0; i < b->by_model.cap; i++) {
        if (!b->by_model.ents[i].key) continue;
        jw_key(out, b->by_model.ents[i].key);
        bucket_write_nested(out, (const SBucket *)b->by_model.ents[i].val);
    }
    jw_obj_end(out);

    jw_key(out, "by_model_realm");
    jw_obj_begin(out);
    for (i = 0; i < b->by_model_realm.cap; i++) {
        const Map *rm;
        if (!b->by_model_realm.ents[i].key) continue;
        jw_key(out, b->by_model_realm.ents[i].key);
        jw_obj_begin(out);
        rm = (const Map *)b->by_model_realm.ents[i].val;
        for (j = 0; j < rm->cap; j++) {
            if (!rm->ents[j].key) continue;
            jw_key(out, rm->ents[j].key);
            bucket_write_nested(out, (const SBucket *)rm->ents[j].val);
        }
        jw_obj_end(out);
    }
    jw_obj_end(out);

    jw_key(out, "by_model_acct");
    jw_obj_begin(out);
    for (i = 0; i < b->by_model_acct.cap; i++) {
        const Map *rm;
        if (!b->by_model_acct.ents[i].key) continue;
        jw_key(out, b->by_model_acct.ents[i].key);
        jw_obj_begin(out);
        rm = (const Map *)b->by_model_acct.ents[i].val;
        for (j = 0; j < rm->cap; j++) {
            const Map *am;
            if (!rm->ents[j].key) continue;
            jw_key(out, rm->ents[j].key);
            jw_obj_begin(out);
            am = (const Map *)rm->ents[j].val;
            for (k = 0; k < am->cap; k++) {
                if (!am->ents[k].key) continue;
                jw_key(out, am->ents[k].key);
                bucket_write_nested(out, (const SBucket *)am->ents[k].val);
            }
            jw_obj_end(out);
        }
        jw_obj_end(out);
    }
    jw_obj_end(out);

    jw_obj_end(out);
}

/* ------------------------------------------------------------------------ */
/* snapshot fold                                                            */
/* ------------------------------------------------------------------------ */

/* requests += 1; then every present USAGE_FIELDS entry (the five token
 * fields plus credit).  Row already applied `row[k] or 0`: a missing or
 * falsy field reads as integer 0, so folding the whole set unconditionally
 * is exactly Python's `if k in row` addition. */
static void bucket_add_present(SBucket *t, const Row *r)
{
    t->requests += 1;
    num_add(&t->prompt, r->prompt, r->prompt_i);
    num_add(&t->completion, r->completion, r->completion_i);
    num_add(&t->reasoning, r->reasoning, r->reasoning_i);
    num_add(&t->cached, r->cached, r->cached_i);
    num_add(&t->total, r->total, r->total_i);
    t->credit += r->credit;
}

static void snapshot_fold_row(const Row *r, const Pricing *pr,
                              const Map *accounts, const char *filter,
                              const char *fallback, SBucket *b)
{
    const char *outcome;
    Cost cost;

    if (filter && strcmp(row_realm_of(r, accounts, fallback), filter) != 0)
        return;
    outcome = row_outcome_of(r);
    /* Every folded row is priced first - a client abort is priced too, its
     * cost is simply never accumulated (and _fold_cost is not called). */
    if (pr) {
        pricing_cost(pr, r->model, r->has_at ? r->at : 0.0,
                     r->prompt, r->cached, r->completion, r->cost_policy,
                     &cost);
    } else {
        cost.known = 0;
        cost.disabled = 0;
        cost.cny = 0.0;
        cost.source = "builtin";
        cost.backfilled = 0;
    }
    if (strcmp(outcome, "completed") != 0) {
        b->errors += 1;
        /* Credit is money already spent: a failed request that was billed
         * still consumed it.  Token totals keep the completed-only rule. */
        if (strcmp(outcome, "client_aborted") != 0) {
            b->credit += r->credit;
            fold_cost(b, &cost, r->model);
        }
        return;
    }
    {
        const char *m = model_key(r->model);
        const char *rr = row_realm_of(r, accounts, fallback);
        int has_acct = r->account && r->account[0];
        int known = cost.known;
        double cny = known ? cost.cny : 0.0;
        SBucket *per, *per_realm, *per_acct;

        bucket_add_present(b, r);
        fold_cost(b, &cost, r->model);
        per = bucket_child(&b->by_model, m);
        per_realm = bucket_child(map_child_map(&b->by_model_realm, m), rr);
        per_acct = bucket_child(
            map_child_map(map_child_map(&b->by_model_acct, m), rr),
            has_acct ? r->account : "(unattributed)");
        bucket_add_present(per, r);
        if (known) per->cost_cny += cny;
        if (has_acct) count_inc(&per->accounts, r->account);
        bucket_add_present(per_realm, r);
        if (known) per_realm->cost_cny += cny;
        if (has_acct) count_inc(&per_realm->accounts, r->account);
        bucket_add_present(per_acct, r);
        if (known) per_acct->cost_cny += cny;
        if (has_acct) count_inc(&per_acct->accounts, r->account);
    }
}

void fold_snapshot(const Row *rows, size_t n, const Pricing *pr,
                   const Map *accounts, const char *default_realm, JW *out)
{
    const char *filter = realm_filter_active(default_realm) ? default_realm : NULL;
    const char *fallback = realm_fallback(default_realm);
    SBucket *b = bucket_new();
    size_t i;

    for (i = 0; i < n; i++)
        snapshot_fold_row(&rows[i], pr, accounts, filter, fallback, b);
    snapshot_write(out, b);
    bucket_free(b);
}

/* ------------------------------------------------------------------------ */
/* count fold (needle scan over raw line text)                              */
/* ------------------------------------------------------------------------ */

/* The count scanner's needle: the literal key text, byte for byte. */
static const char REALM_KEY[] = "\"realm\"";
static const char REALM_SPACED[] = ": \"";
static const char REALM_TIGHT[] = ":\"";

/* Byte-level `str.find` with a start offset; (size_t)-1 when absent.
 * memchr for the first byte, memcmp only on a hit. */
static size_t find_bytes(const char *hay, size_t len, size_t from,
                         const char *needle, size_t nlen)
{
    const char *hit;
    if (from > len || len - from < nlen) return (size_t)-1;
    hit = (const char *)memchr(hay + from, needle[0], len - from - nlen + 1);
    while (hit) {
        if (memcmp(hit, needle, nlen) == 0) return (size_t)(hit - hay);
        hit = (const char *)memchr(hit + 1, needle[0],
                                   (hay + len - nlen + 1) - (hit + 1));
    }
    return (size_t)-1;
}

void fold_count(const char *const *lines, const size_t *lens, size_t n,
                const Map *accounts, const char *default_realm, JW *out)
{
    WBCount *counter = count_begin(accounts, default_realm);
    size_t i;
    for (i = 0; i < n; i++) count_feed(counter, lines[i], lens[i]);
    count_finish(counter, out);
}

/* ------------------------------------------------------------------------ */
/* streaming count state (same needle rules, no line array kept)             */
/* ------------------------------------------------------------------------ */

struct WBCount {
    const Map *accounts;
    const char *fallback;
    Map n_realm;
    int64_t total;
};

WBCount *count_begin(const Map *accounts, const char *default_realm)
{
    WBCount *c = (WBCount *)xcalloc(1, sizeof(*c));
    c->accounts = accounts;
    c->fallback = realm_fallback(default_realm);
    map_init(&c->n_realm);
    return c;
}

void count_feed(WBCount *c, const char *line, size_t len)
{
    size_t pos;
    c->total += 1;
    pos = find_bytes(line, len, 0, REALM_KEY, sizeof REALM_KEY - 1);
    if (pos == (size_t)-1) {
        /* No realm key text anywhere (old error rows): parse and fall back
         * through row_realm - account, then model, then default.  An
         * unparseable line (or a non-object) only counts toward n. */
        JVal *tree = jparse(line, len);
        if (tree) {
            if (jis_obj(tree)) {
                Row row;
                row_from_json(&row, tree);
                count_inc(&c->n_realm, row_realm_of(&row, c->accounts, c->fallback));
            }
            jfree(tree);
        }
        return;
    }
    while (pos != (size_t)-1) {
        size_t end = pos + (sizeof REALM_KEY - 1);
        size_t start = (size_t)-1;
        size_t quote;
        if (find_bytes(line, len, end, REALM_SPACED, sizeof REALM_SPACED - 1) == end)
            start = end + sizeof REALM_SPACED - 1;
        else if (find_bytes(line, len, end, REALM_TIGHT, sizeof REALM_TIGHT - 1) == end)
            start = end + sizeof REALM_TIGHT - 1;
        if (start != (size_t)-1) {
            quote = find_bytes(line, len, start, "\"", 1);
            if (quote != (size_t)-1)
                count_inc_slice(&c->n_realm, line + start, quote - start);
        }
        pos = find_bytes(line, len, end, REALM_KEY, sizeof REALM_KEY - 1);
    }
}

void count_finish(WBCount *c, JW *out)
{
    jw_obj_begin(out);
    jw_key(out, "n");
    jw_int(out, (long long)c->total);
    jw_count_map(out, "n_realm", &c->n_realm);
    jw_obj_end(out);
    count_map_free(&c->n_realm);
    free(c);
}

/* ------------------------------------------------------------------------ */
/* daily fold                                                               */
/* ------------------------------------------------------------------------ */

static void daily_free(Map *tokens, Map *credits, Map *models)
{
    size_t i, j;
    for (i = 0; i < tokens->cap; i++) free(tokens->ents[i].val);
    map_free(tokens);
    for (i = 0; i < credits->cap; i++) free(credits->ents[i].val);
    map_free(credits);
    for (i = 0; i < models->cap; i++) {
        Map *per;
        if (!models->ents[i].key) continue;
        per = (Map *)models->ents[i].val;
        for (j = 0; j < per->cap; j++) free(per->ents[j].val);
        map_free(per);
        free(per);
    }
    map_free(models);
}

void fold_daily(const Row *rows, size_t n, double midnight, JW *out)
{
    Map tokens;   /* uid -> Num*                    */
    Map credits;  /* uid -> double*                 */
    Map models;   /* uid -> Map* (model -> Num*)    */
    size_t i, j;

    map_init(&tokens);
    map_init(&credits);
    map_init(&models);
    for (i = 0; i < n; i++) {
        const Row *r = &rows[i];
        double at = r->has_at ? r->at : 0.0;
        Num *tk;
        if (at < midnight) continue;
        /* Same rule as the analytics scan: a client cancellation is not a
         * consumed request and its token counts are incomplete. */
        if (strcmp(row_outcome_of(r), "client_aborted") == 0) continue;
        if (!r->account || !r->account[0]) continue;
        tk = num_child(&tokens, r->account);
        num_add(tk, r->total, r->total_i);
        if (r->credit != 0.0)
            *double_child(&credits, r->account) += r->credit;
        if (r->model && r->model[0]) {
            Map *per = map_child_map(&models, r->account);
            num_add(num_child(per, r->model), r->total, r->total_i);
        }
    }
    jw_obj_begin(out);
    jw_key(out, "tokens");
    jw_obj_begin(out);
    for (i = 0; i < tokens.cap; i++) {
        if (!tokens.ents[i].key) continue;
        jw_key(out, tokens.ents[i].key);
        jw_num_value(out, (const Num *)tokens.ents[i].val);
    }
    jw_obj_end(out);
    jw_key(out, "credits");
    jw_obj_begin(out);
    for (i = 0; i < credits.cap; i++) {
        if (!credits.ents[i].key) continue;
        jw_key(out, credits.ents[i].key);
        jw_double(out, *(const double *)credits.ents[i].val);
    }
    jw_obj_end(out);
    jw_key(out, "models");
    jw_obj_begin(out);
    for (i = 0; i < models.cap; i++) {
        const Map *per;
        if (!models.ents[i].key) continue;
        jw_key(out, models.ents[i].key);
        jw_obj_begin(out);
        per = (const Map *)models.ents[i].val;
        for (j = 0; j < per->cap; j++) {
            if (!per->ents[j].key) continue;
            jw_key(out, per->ents[j].key);
            jw_num_value(out, (const Num *)per->ents[j].val);
        }
        jw_obj_end(out);
    }
    jw_obj_end(out);
    jw_obj_end(out);
    daily_free(&tokens, &credits, &models);
}
