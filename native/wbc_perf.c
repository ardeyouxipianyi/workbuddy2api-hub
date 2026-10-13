/* wbc_perf.c -- standalone perf fold for the C aggregation engine.
 *
 * Parity target: wb_proxy._perf_stats_uncached(sample, realm, since, until)
 * over _tail_lines(log, sample), i.e. the last `sample` non-blank log lines
 * plus the trailing no-newline fragment (which counts as one sample line).
 * The caller passes exactly that tail as stripped text; this file rebuilds
 * the same aggregates the Python fold builds, including the incremental
 * _perf_push / _perf_drop_oldest / _perf_apply machinery the cache layer
 * uses, so over-fed input behaves like the incremental path too.
 *
 * The ring of entries and every per-metric value list are head/len ring
 * buffers: a value enters at the tail, the oldest row leaves at the head (a
 * ring drop) and the newest at the tail (re-folding the trailing fragment).
 * Iteration is always ring order (oldest first), which is the order the
 * Python fold sums in.
 *
 * Buckets are deleted when their total reaches zero (m -> mr -> ma, like the
 * Python del statements) and reappear at their new first appearance, which
 * the payload's ring scan restores.
 *
 * Documented divergences (real logs never carry these shapes):
 *   - a present but non-numeric at / model / realm / account / elapsed_ms /
 *     ttft_ms / gen_ms / tokens_per_sec / cache_hit_pct reads as absent,
 *     like Row does (the Python fold would carry the raw value);
 *   - float() tokens are matched on ASCII: PEP 515 underscores between
 *     digits and inf/nan spellings are accepted, non-ASCII digit forms are
 *     not.
 *
 * C99, no external dependencies.
 */

#include "wbc_json.h"
#include "wbc_row.h"
#include "wbc_util.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Same failure policy as the rest of the engine. */
static void pf_oom(void)
{
    fprintf(stderr, "wb-agg: out of memory\n");
    exit(3);
}

static char *pf_strdup(const char *s)
{
    size_t n;
    char *out;
    if (!s) s = "";
    n = strlen(s) + 1;
    out = (char *)malloc(n);
    if (!out) pf_oom();
    memcpy(out, s, n);
    return out;
}

/* ---- one metric's raw sample list -------------------------------------- */

/* Python keeps a deque per metric: values enter at the tail; withdrawing a
 * row pops the oldest value (head) or the newest (tail, when the trailing
 * partial is re-folded).  A head/len ring buffer does the same in O(1) per
 * operation; iteration is oldest -> newest. */
typedef struct {
    double *vals;
    size_t head;
    size_t len;
    size_t cap;
} PfList;

static void pf_list_init(PfList *l)
{
    l->vals = NULL;
    l->head = l->len = l->cap = 0;
}

static void pf_list_free(PfList *l)
{
    free(l->vals);
    pf_list_init(l);
}

static double pf_list_at(const PfList *l, size_t i)
{
    return l->vals[(l->head + i) % l->cap];
}

static void pf_list_push(PfList *l, double v)
{
    if (l->len == l->cap) {
        size_t cap = l->cap ? l->cap * 2 : 16;
        double *fresh = (double *)malloc(cap * sizeof(double));
        size_t i;
        if (!fresh) pf_oom();
        for (i = 0; i < l->len; i++) fresh[i] = pf_list_at(l, i);
        free(l->vals);
        l->vals = fresh;
        l->head = 0;
        l->cap = cap;
    }
    l->vals[(l->head + l->len) % l->cap] = v;
    l->len++;
}

static void pf_list_pop_front(PfList *l)
{
    if (!l->len) return;
    l->head = (l->head + 1) % l->cap;
    l->len--;
}

static void pf_list_pop_back(PfList *l)
{
    if (!l->len) return;
    l->len--;
}

/* ---- buckets and the aggregate ----------------------------------------- */

/* One aggregation bucket (global, model, model+realm, model+realm+account).
 * `walls` also collects the elapsed_ms of failed and aborted rows, exactly
 * like the Python buckets. */
typedef struct {
    long long total;
    long long ok;
    long long err;
    long long aborted;
    PfList ttfts, gens, walls, tok_rates, hits;
} PfBucket;

static PfBucket *pf_bucket_new(void)
{
    PfBucket *b = (PfBucket *)calloc(1, sizeof(PfBucket));
    if (!b) pf_oom();
    pf_list_init(&b->ttfts);
    pf_list_init(&b->gens);
    pf_list_init(&b->walls);
    pf_list_init(&b->tok_rates);
    pf_list_init(&b->hits);
    return b;
}

static void pf_bucket_free(PfBucket *b)
{
    if (!b) return;
    pf_list_free(&b->ttfts);
    pf_list_free(&b->gens);
    pf_list_free(&b->walls);
    pf_list_free(&b->tok_rates);
    pf_list_free(&b->hits);
    free(b);
}

typedef struct {
    long long total;
    long long ok;
    long long err;
    long long aborted;
    PfList ttfts, gens, walls, tok_rates, hits;
    Map m;    /* model -> PfBucket* */
    Map mr;   /* model -> Map* (realm -> PfBucket*) */
    Map ma;   /* model -> Map* (realm -> Map* (acct -> PfBucket*)) */
} PfAgg;

static void pf_agg_init(PfAgg *a)
{
    memset(a, 0, sizeof(*a));
    pf_list_init(&a->ttfts);
    pf_list_init(&a->gens);
    pf_list_init(&a->walls);
    pf_list_init(&a->tok_rates);
    pf_list_init(&a->hits);
    map_init(&a->m);
    map_init(&a->mr);
    map_init(&a->ma);
}

static void pf_agg_free(PfAgg *a)
{
    size_t i, j, k;
    for (i = 0; i < a->m.cap; i++)
        pf_bucket_free((PfBucket *)a->m.ents[i].val);
    map_free(&a->m);
    for (i = 0; i < a->mr.cap; i++) {
        Map *realms = (Map *)a->mr.ents[i].val;
        if (!realms) continue;
        for (j = 0; j < realms->cap; j++)
            pf_bucket_free((PfBucket *)realms->ents[j].val);
        map_free(realms);
        free(realms);
    }
    map_free(&a->mr);
    for (i = 0; i < a->ma.cap; i++) {
        Map *realms = (Map *)a->ma.ents[i].val;
        if (!realms) continue;
        for (j = 0; j < realms->cap; j++) {
            Map *accts = (Map *)realms->ents[j].val;
            if (!accts) continue;
            for (k = 0; k < accts->cap; k++)
                pf_bucket_free((PfBucket *)accts->ents[k].val);
            map_free(accts);
            free(accts);
        }
        map_free(realms);
        free(realms);
    }
    map_free(&a->ma);
    pf_list_free(&a->ttfts);
    pf_list_free(&a->gens);
    pf_list_free(&a->walls);
    pf_list_free(&a->tok_rates);
    pf_list_free(&a->hits);
}

static Map *pf_map_new(void)
{
    Map *m = (Map *)malloc(sizeof(Map));
    if (!m) pf_oom();
    map_init(m);
    return m;
}

/* FNV-1a, the same constants as the map in wbc_util.c: the probe below must
 * hash exactly like map_slot() does. */
static uint64_t pf_hash(const char *key)
{
    uint64_t hash = 1469598103934665603ULL;
    const unsigned char *p = (const unsigned char *)key;
    while (*p) {
        hash ^= *p++;
        hash *= 1099511628211ULL;
    }
    return hash;
}

/* Remove one key (Python `del`) and hand its value back.  The shared Map has
 * no delete, and an open-addressed map cannot have its probe chain cut by a
 * bare tombstone, so the survivors are rehashed.  O(cap) per deletion, and
 * the perf maps hold one entry per model / realm / account. */
static void *pf_map_take(Map *m, const char *key)
{
    size_t mask, i, n = 0;
    MapEnt *kept;
    void *val;
    if (!m->cap) return NULL;
    mask = m->cap - 1;
    i = (size_t)pf_hash(key) & mask;
    while (m->ents[i].key && strcmp(m->ents[i].key, key) != 0)
        i = (i + 1) & mask;
    if (!m->ents[i].key) return NULL;
    val = m->ents[i].val;
    free(m->ents[i].key);
    m->ents[i].key = NULL;
    m->ents[i].val = NULL;
    kept = (MapEnt *)malloc(sizeof(MapEnt) * (m->len ? m->len : 1));
    if (!kept) pf_oom();
    for (i = 0; i < m->cap; i++) {
        if (m->ents[i].key) {
            kept[n].key = m->ents[i].key;
            kept[n].val = m->ents[i].val;
            n++;
            m->ents[i].key = NULL;
            m->ents[i].val = NULL;
        }
    }
    m->len = 0;
    for (i = 0; i < n; i++) {
        map_put(m, kept[i].key, kept[i].val);
        free(kept[i].key);
    }
    free(kept);
    return val;
}

/* ---- ring entries ------------------------------------------------------ */

enum {
    PF_ENTRY_NONE = 0,   /* bad JSON: no contribution, no at */
    PF_ENTRY_SHORT = 1,  /* parsed but filtered: carries at only */
    PF_ENTRY_FULL = 2    /* the 10-tuple: contributes to the aggregates */
};

enum {
    PF_OUTCOME_COMPLETED = 0,
    PF_OUTCOME_ABORTED = 1,
    PF_OUTCOME_OTHER = 2
};

typedef struct {
    int kind;
    int has_at;
    double at;
    /* PF_ENTRY_FULL only; the strings are owned by the entry */
    char *model;
    char *realm;
    char *acct;
    int outcome;
    int has_elapsed;
    double elapsed;
    int has_ttft;
    double ttft;
    int has_gen;
    double gen;
    int has_tps;
    double tps;
    int has_hit;
    double hit;
} PfEntry;

static void pf_entry_clear(PfEntry *e)
{
    free(e->model);
    free(e->realm);
    free(e->acct);
    memset(e, 0, sizeof(*e));
}

/* The window the fold runs under: has_* say the caller passed a bound; 0
 * means no lower bound and 1e18 no upper bound, mirroring range_window()'s
 * None sides. */
typedef struct {
    int use_since;
    double since;
    int use_until;
    double until;
} PfWin;

/* ---- the line-text predicates (wb_proxy:1250, wb_proxy:4564) ----------- */

static int pf_is_pyspace(char c)
{
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' ||
           c == '\v' || c == '\f';
}

static int pf_is_digit(char c)
{
    return c >= '0' && c <= '9';
}

/* Python float(token): surrounding whitespace allowed, PEP 515 underscores
 * between digits allowed, inf/infinity/nan accepted.  Hex literals are not
 * (strtod would take them). */
static int pf_float_token(const char *tok, size_t len, double *out)
{
    size_t i = 0, k, n = 0;
    char buf[64];
    char *clean = buf;
    int heap = 0;
    char *endp;
    double v;

    while (i < len && pf_is_pyspace(tok[i])) i++;
    while (len > i && pf_is_pyspace(tok[len - 1])) len--;
    if (i >= len) return 0;
    if (len - i + 1 > sizeof(buf)) {
        clean = (char *)malloc(len - i + 1);
        if (!clean) pf_oom();
        heap = 1;
    }
    for (k = i; k < len; k++) {
        char c = tok[k];
        if (c == '_') {
            if (k == i || k + 1 >= len ||
                !pf_is_digit(tok[k - 1]) || !pf_is_digit(tok[k + 1])) {
                if (heap) free(clean);
                return 0;
            }
            continue;   /* valid digit separator: dropped before strtod */
        }
        clean[n++] = c;
    }
    clean[n] = '\0';
    {
        const char *p = clean;
        if (*p == '+' || *p == '-') p++;
        if (p[0] == '0' && (p[1] == 'x' || p[1] == 'X')) {
            if (heap) free(clean);
            return 0;
        }
    }
    v = strtod(clean, &endp);
    if (endp == clean) {
        if (heap) free(clean);
        return 0;
    }
    while (*endp && pf_is_pyspace(*endp)) endp++;
    if (*endp) {
        if (heap) free(clean);
        return 0;
    }
    if (heap) free(clean);
    *out = v;
    return 1;
}

static int pf_memmem(const char *hay, size_t hay_len,
                     const char *needle, size_t needle_len)
{
    size_t i;
    if (needle_len == 0) return 1;
    if (hay_len < needle_len) return 0;
    for (i = 0; i + needle_len <= hay_len; i++) {
        if (hay[i] == needle[0] && memcmp(hay + i, needle, needle_len) == 0)
            return 1;
    }
    return 0;
}

/* _AT_PREFIX, 7 bytes: every row this process writes starts with it. */
static const char PF_AT_PREFIX[] = "{\"at\": ";

/* _perf_at_token: the float a pre-filtered line declares, or no value. */
static int pf_at_token(const char *line, size_t len, double *out)
{
    size_t i, end, toklen;
    if (len < 7 || memcmp(line, PF_AT_PREFIX, 7) != 0) return 0;
    for (i = 7; i < len; i++)
        if (line[i] == ',') break;
    if (i >= len) return 0;
    end = i;
    toklen = end - 7;
    if (!memchr(line + 7, '.', toklen) && !memchr(line + 7, 'e', toklen) &&
        !memchr(line + 7, 'E', toklen))
        return 0;
    return pf_float_token(line + 7, toklen, out);
}

/* _line_outside_window: True only when this line's `at` is provably outside
 * [lo, hi].  Such a line is dropped by the window check below anyway, so
 * skipping it only saves the parse; the declared at is still kept for
 * sample_from. */
static int pf_line_outside_window(const char *line, size_t len, const PfWin *win)
{
    size_t i, end, toklen;
    double at;
    if (len < 7 || memcmp(line, PF_AT_PREFIX, 7) != 0) return 0;
    for (i = 7; i < len; i++)
        if (line[i] == ',') break;
    if (i >= len) return 0;
    end = i;
    toklen = end - 7;
    if (!memchr(line + 7, '.', toklen) && !memchr(line + 7, 'e', toklen) &&
        !memchr(line + 7, 'E', toklen))
        return 0;
    if (!pf_float_token(line + 7, toklen, &at)) return 0;
    if (!((win->use_since && at < win->since) ||
          (win->use_until && at > win->until)))
        return 0;
    /* json keeps the last duplicate key: a later "at" would make the first
     * value read here the wrong one, so those lines are parsed instead. */
    if (pf_memmem(line + end, len - end, "\"at\"", 4)) return 0;
    return 1;
}

/* ---- _perf_apply ------------------------------------------------------- */

/* One numeric contribution: append at the tail when folding in, pop the
 * matching head (oldest row slid out) or tail (partial re-folded) when
 * withdrawing. */
static void pf_put(PfList *vals, double value, int add, int front)
{
    if (add > 0) pf_list_push(vals, value);
    else if (front) pf_list_pop_front(vals);
    else pf_list_pop_back(vals);
}

static void pf_apply(PfAgg *agg, const PfEntry *e, int add, int front)
{
    PfBucket *mb, *rb, *ab;
    Map *realms, *accts, *by_realm;

    if (!e || e->kind != PF_ENTRY_FULL) return;
    if (add > 0) {
        mb = (PfBucket *)map_get(&agg->m, e->model);
        if (!mb) {
            mb = pf_bucket_new();
            map_put(&agg->m, e->model, mb);
        }
        realms = (Map *)map_get(&agg->mr, e->model);
        if (!realms) {
            realms = pf_map_new();
            map_put(&agg->mr, e->model, realms);
        }
        rb = (PfBucket *)map_get(realms, e->realm);
        if (!rb) {
            rb = pf_bucket_new();
            map_put(realms, e->realm, rb);
        }
        accts = (Map *)map_get(&agg->ma, e->model);
        if (!accts) {
            accts = pf_map_new();
            map_put(&agg->ma, e->model, accts);
        }
        by_realm = (Map *)map_get(accts, e->realm);
        if (!by_realm) {
            by_realm = pf_map_new();
            map_put(accts, e->realm, by_realm);
        }
        ab = (PfBucket *)map_get(by_realm, e->acct);
        if (!ab) {
            ab = pf_bucket_new();
            map_put(by_realm, e->acct, ab);
        }
    } else {
        realms = (Map *)map_get(&agg->mr, e->model);
        accts = (Map *)map_get(&agg->ma, e->model);
        by_realm = accts ? (Map *)map_get(accts, e->realm) : NULL;
        mb = (PfBucket *)map_get(&agg->m, e->model);
        rb = realms ? (PfBucket *)map_get(realms, e->realm) : NULL;
        ab = by_realm ? (PfBucket *)map_get(by_realm, e->acct) : NULL;
        if (!mb || !rb || !ab) return;   /* a live entry always has buckets */
    }
    agg->total += add;
    mb->total += add;
    rb->total += add;
    ab->total += add;
    /* A client that walks away is not a gateway failure: it gets its own
     * count instead of dragging the success rate down.  elapsed walks the
     * truthiness test here and the is-not-None test on the ok path, exactly
     * like the Python fold. */
    if (e->outcome == PF_OUTCOME_ABORTED) {
        agg->aborted += add;
        mb->aborted += add;
        rb->aborted += add;
        ab->aborted += add;
        if (e->has_elapsed && e->elapsed != 0.0) {
            pf_put(&mb->walls, e->elapsed, add, front);
            pf_put(&rb->walls, e->elapsed, add, front);
            pf_put(&ab->walls, e->elapsed, add, front);
            pf_put(&agg->walls, e->elapsed, add, front);
        }
    } else if (e->outcome != PF_OUTCOME_COMPLETED) {
        agg->err += add;
        mb->err += add;
        rb->err += add;
        ab->err += add;
        if (e->has_elapsed && e->elapsed != 0.0) {
            pf_put(&mb->walls, e->elapsed, add, front);
            pf_put(&rb->walls, e->elapsed, add, front);
            pf_put(&ab->walls, e->elapsed, add, front);
            pf_put(&agg->walls, e->elapsed, add, front);
        }
    } else {
        agg->ok += add;
        mb->ok += add;
        rb->ok += add;
        ab->ok += add;
        if (e->has_ttft) {
            pf_put(&agg->ttfts, e->ttft, add, front);
            pf_put(&mb->ttfts, e->ttft, add, front);
            pf_put(&rb->ttfts, e->ttft, add, front);
            pf_put(&ab->ttfts, e->ttft, add, front);
        }
        if (e->has_gen) {
            pf_put(&agg->gens, e->gen, add, front);
            pf_put(&mb->gens, e->gen, add, front);
            pf_put(&rb->gens, e->gen, add, front);
            pf_put(&ab->gens, e->gen, add, front);
        }
        if (e->has_elapsed) {
            pf_put(&agg->walls, e->elapsed, add, front);
            pf_put(&mb->walls, e->elapsed, add, front);
            pf_put(&rb->walls, e->elapsed, add, front);
            pf_put(&ab->walls, e->elapsed, add, front);
        }
        if (e->has_tps && e->tps != 0.0) {
            pf_put(&agg->tok_rates, e->tps, add, front);
            pf_put(&mb->tok_rates, e->tps, add, front);
            pf_put(&rb->tok_rates, e->tps, add, front);
            pf_put(&ab->tok_rates, e->tps, add, front);
        }
        if (e->has_hit) {
            pf_put(&agg->hits, e->hit, add, front);
            pf_put(&mb->hits, e->hit, add, front);
            pf_put(&rb->hits, e->hit, add, front);
            pf_put(&ab->hits, e->hit, add, front);
        }
    }
    if (add < 0) {
        /* Empty buckets are deleted, in the Python order: m, then mr (realm
         * then model), then ma (account, realm, model).  A later row for the
         * same key builds a fresh bucket, and the payload's ring scan puts
         * it back at its new first appearance. */
        if (mb->total == 0) {
            pf_map_take(&agg->m, e->model);
            pf_bucket_free(mb);
        }
        if (rb->total == 0) {
            Map *r2 = (Map *)map_get(&agg->mr, e->model);
            if (r2) {
                pf_map_take(r2, e->realm);
                if (r2->len == 0) {
                    pf_map_take(&agg->mr, e->model);
                    map_free(r2);
                    free(r2);
                }
            }
            pf_bucket_free(rb);
        }
        if (ab->total == 0) {
            Map *a2 = (Map *)map_get(&agg->ma, e->model);
            Map *b2 = a2 ? (Map *)map_get(a2, e->realm) : NULL;
            if (b2) {
                pf_map_take(b2, e->acct);
                if (b2->len == 0) {
                    pf_map_take(a2, e->realm);
                    map_free(b2);
                    free(b2);
                }
            }
            if (a2 && a2->len == 0) {
                pf_map_take(&agg->ma, e->model);
                map_free(a2);
                free(a2);
            }
            pf_bucket_free(ab);
        }
    }
}

/* ---- _perf_line_entry -------------------------------------------------- */

static void pf_opt_num(const JVal *obj, const char *field, double *v, int *has)
{
    const JVal *x = jget(obj, field);
    if (x && jis_num(x)) {
        *v = jnum(x);
        *has = 1;
    } else {
        *v = 0.0;
        *has = 0;
    }
}

/* row_matches_realm: None/""/"all" means every realm. */
static int pf_realm_filter_active(const char *realm_filter)
{
    return realm_filter && realm_filter[0] && strcmp(realm_filter, "all") != 0;
}

/* Fold one line into a ring entry (the caller has already made room and has
 * not counted this line yet). */
static void pf_line_entry(PfEntry *e, const char *line, size_t len,
                          const PfWin *win, size_t n_at,
                          const char *realm_filter, const Map *accounts,
                          const char *default_realm)
{
    JVal *tree;
    Row r;
    const char *dr = default_realm ? default_realm : "intl";
    double atv;

    memset(e, 0, sizeof(*e));
    /* The same cheap pre-filter as the Python path: it only drops lines the
     * window check below would drop too, but their declared at still counts
     * for sample_from.  A half-written line (no closing brace) is parsed
     * instead; bad JSON is parsed and fails, like the Python path. */
    if ((win->use_since || win->use_until) && n_at > 0 && len > 0 &&
        line[len - 1] == '}' && pf_line_outside_window(line, len, win)) {
        double at_tok;
        e->kind = PF_ENTRY_SHORT;
        e->has_at = pf_at_token(line, len, &at_tok);
        if (e->has_at) e->at = at_tok;
        return;
    }
    tree = jparse(line, len);
    if (!tree) {
        e->kind = PF_ENTRY_NONE;
        return;
    }
    if (!jis_obj(tree)) {
        /* Python would raise on r.get() here; a non-object is unusable. */
        jfree(tree);
        e->kind = PF_ENTRY_NONE;
        return;
    }
    row_from_json(&r, tree);
    e->has_at = r.has_at;
    e->at = r.at;
    if (pf_realm_filter_active(realm_filter)) {
        const char *rr = row_realm_of(&r, accounts, dr);
        if (!rr || strcmp(rr, realm_filter) != 0) {
            jfree(tree);
            e->kind = PF_ENTRY_SHORT;
            return;
        }
    }
    atv = r.has_at ? r.at : 0.0;
    if ((win->use_since && atv < win->since) ||
        (win->use_until && atv > win->until)) {
        jfree(tree);
        e->kind = PF_ENTRY_SHORT;
        return;
    }
    e->kind = PF_ENTRY_FULL;
    e->model = pf_strdup((r.model && r.model[0]) ? r.model : "unknown");
    e->realm = pf_strdup(row_realm_of(&r, accounts, dr));
    e->acct = pf_strdup((r.account && r.account[0]) ? r.account
                                                    : "(unattributed)");
    {
        const char *oc = row_outcome_of(&r);
        e->outcome = !strcmp(oc, "client_aborted") ? PF_OUTCOME_ABORTED
                   : !strcmp(oc, "completed") ? PF_OUTCOME_COMPLETED
                   : PF_OUTCOME_OTHER;
    }
    pf_opt_num(tree, "elapsed_ms", &e->elapsed, &e->has_elapsed);
    pf_opt_num(tree, "ttft_ms", &e->ttft, &e->has_ttft);
    pf_opt_num(tree, "gen_ms", &e->gen, &e->has_gen);
    pf_opt_num(tree, "tokens_per_sec", &e->tps, &e->has_tps);
    pf_opt_num(tree, "cache_hit_pct", &e->hit, &e->has_hit);
    jfree(tree);
}

/* ---- the ring of entries ----------------------------------------------- */

typedef struct {
    PfEntry *ents;
    size_t head;
    size_t len;
    size_t cap;          /* min(sample, n): the ring never holds more */
    long long sample;
    size_t n_at;         /* entries whose at is not None */
} PfRing;

static PfEntry *pf_ring_at(PfRing *r, size_t i)
{
    return &r->ents[(r->head + i) % r->cap];
}

static void pf_ring_grow(PfRing *ring)
{
    size_t cap = ring->cap ? ring->cap * 2 : 16;
    PfEntry *fresh = (PfEntry *)malloc(cap * sizeof(PfEntry));
    size_t i;
    if (!fresh) pf_oom();
    for (i = 0; i < ring->len; i++) fresh[i] = *pf_ring_at(ring, i);
    free(ring->ents);
    ring->ents = fresh;
    ring->head = 0;
    ring->cap = cap;
}

/* _perf_drop_oldest */
static void pf_drop_oldest(PfRing *ring, PfAgg *agg)
{
    PfEntry *old;
    if (!ring->len) return;
    old = pf_ring_at(ring, 0);
    if (old->kind != PF_ENTRY_NONE && old->has_at) ring->n_at--;
    pf_apply(agg, old, -1, 1);
    pf_entry_clear(old);
    ring->head = (ring->head + 1) % ring->cap;
    ring->len--;
}

/* _perf_push: make room first, then fold the line and fold it in.  Making
 * room first matters: the Python pre-filter reads the ring's current at
 * count, so the row sliding out must already be gone. */
static void pf_push_line(PfRing *ring, PfAgg *agg, const char *line, size_t len,
                         const PfWin *win, const char *realm_filter,
                         const Map *accounts, const char *default_realm)
{
    PfEntry e;
    while ((long long)ring->len >= ring->sample) pf_drop_oldest(ring, agg);
    pf_line_entry(&e, line, len, win, ring->n_at, realm_filter, accounts,
                  default_realm);
    if (e.kind != PF_ENTRY_NONE && e.has_at) ring->n_at++;
    pf_apply(agg, &e, 1, 0);
    if (ring->len == ring->cap) pf_ring_grow(ring);
    ring->ents[(ring->head + ring->len) % ring->cap] = e;
    ring->len++;
}

/* ---- _perf_block ------------------------------------------------------- */

static int pf_cmp_double(const void *a, const void *b)
{
    double x = *(const double *)a;
    double y = *(const double *)b;
    if (x < y) return -1;
    if (x > y) return 1;
    return 0;
}

/* rank(q) = ordered[clamp(int(round(q/100*(count-1))))], half to even. */
static double pf_rank(const double *ordered, size_t count, double q)
{
    double x = (q / 100.0) * (double)(count - 1);
    long long idx = (long long)py_round_half_even(x);
    if (idx < 0) idx = 0;
    if (idx > (long long)(count - 1)) idx = (long long)(count - 1);
    return ordered[idx];
}

/* One metric's summary block, straight from the raw samples: avg sums in
 * ring order (the Python fold's list order), percentiles come off one sorted
 * copy.  Empty -> null. */
static void pf_emit_block(JW *jw, const PfList *l)
{
    size_t i, n = l->len;
    double *ordered, sum = 0.0, mx;
    if (n == 0) {
        jw_null(jw);
        return;
    }
    ordered = (double *)malloc(n * sizeof(double));
    if (!ordered) pf_oom();
    for (i = 0; i < n; i++) {
        double v = pf_list_at(l, i);
        ordered[i] = v;
        sum += v;
    }
    mx = ordered[0];
    for (i = 1; i < n; i++)
        if (ordered[i] > mx) mx = ordered[i];
    qsort(ordered, n, sizeof(double), pf_cmp_double);
    jw_obj_begin(jw);
    jw_key(jw, "avg");
    jw_double(jw, py_round1(sum / (double)n));
    jw_key(jw, "p50");
    jw_double(jw, pf_rank(ordered, n, 50.0));
    jw_key(jw, "p90");
    jw_double(jw, pf_rank(ordered, n, 90.0));
    jw_key(jw, "p99");
    jw_double(jw, pf_rank(ordered, n, 99.0));
    jw_key(jw, "max");
    jw_double(jw, mx);
    jw_key(jw, "samples");
    jw_int(jw, (long long)n);
    jw_obj_end(jw);
    free(ordered);
}

static double pf_rate(long long ok, long long err)
{
    return py_round1((double)ok * 100.0 / (double)(ok + err));
}

static void pf_emit_bucket(JW *jw, const PfBucket *b)
{
    static const PfList none = { NULL, 0, 0, 0 };
    const PfList *ttfts = b ? &b->ttfts : &none;
    const PfList *gens = b ? &b->gens : &none;
    const PfList *walls = b ? &b->walls : &none;
    const PfList *rates = b ? &b->tok_rates : &none;
    const PfList *hits = b ? &b->hits : &none;
    long long total = b ? b->total : 0;
    long long err = b ? b->err : 0;
    long long aborted = b ? b->aborted : 0;
    long long ok = b ? b->ok : 0;

    jw_obj_begin(jw);
    jw_key(jw, "requests");
    jw_int(jw, total);
    jw_key(jw, "errors");
    jw_int(jw, err);
    jw_key(jw, "client_aborted");
    jw_int(jw, aborted);
    jw_key(jw, "success_rate_pct");
    if (ok + err) jw_double(jw, pf_rate(ok, err));
    else jw_null(jw);
    jw_key(jw, "ttft_ms");
    pf_emit_block(jw, ttfts);
    jw_key(jw, "generation_ms");
    pf_emit_block(jw, gens);
    jw_key(jw, "wall_ms");
    pf_emit_block(jw, walls);
    jw_key(jw, "tokens_per_sec");
    pf_emit_block(jw, rates);
    jw_key(jw, "cache_hit_pct");
    pf_emit_block(jw, hits);
    jw_obj_end(jw);
}

/* ---- _perf_payload ----------------------------------------------------- */

/* Bucket order restored from the ring: first appearance of model, then of
 * (model, realm), then of (model, realm, account).  A full rebuild builds
 * the dicts in exactly this order, and the incremental path re-creates a
 * deleted bucket at its new first appearance, so the ring scan matches both.
 * Nodes are individually allocated so the ordering arrays can grow. */
typedef struct PfAcctNode {
    const char *name;      /* borrowed from a live ring entry */
} PfAcctNode;

typedef struct PfRealmNode {
    const char *name;
    PfAcctNode **accts;
    size_t nacct, cacct;
    Map ix;                /* account name -> PfAcctNode* */
} PfRealmNode;

typedef struct PfModelNode {
    const char *name;
    PfRealmNode **realms;
    size_t nrealm, crealm;
    Map ix;                /* realm name -> PfRealmNode* */
} PfModelNode;

static void pf_payload(const PfAgg *agg, PfRing *ring, JW *out)
{
    Map model_ix;
    PfModelNode **models = NULL;
    size_t nmodels = 0, cmodels = 0;
    size_t i, j, k;
    int have_from = 0;
    double from = 0.0;

    map_init(&model_ix);
    for (i = 0; i < ring->len; i++) {
        PfEntry *e = pf_ring_at(ring, i);
        PfModelNode *mn;
        PfRealmNode *rn;
        if (e->kind != PF_ENTRY_NONE && e->has_at && !have_from) {
            have_from = 1;
            from = e->at;
        }
        if (e->kind != PF_ENTRY_FULL) continue;
        mn = (PfModelNode *)map_get(&model_ix, e->model);
        if (!mn) {
            if (nmodels == cmodels) {
                cmodels = cmodels ? cmodels * 2 : 8;
                models = (PfModelNode **)realloc(models,
                                                 cmodels * sizeof(PfModelNode *));
                if (!models) pf_oom();
            }
            mn = (PfModelNode *)malloc(sizeof(PfModelNode));
            if (!mn) pf_oom();
            mn->name = e->model;
            mn->realms = NULL;
            mn->nrealm = mn->crealm = 0;
            map_init(&mn->ix);
            map_put(&model_ix, e->model, mn);
            models[nmodels++] = mn;
        }
        rn = (PfRealmNode *)map_get(&mn->ix, e->realm);
        if (!rn) {
            if (mn->nrealm == mn->crealm) {
                mn->crealm = mn->crealm ? mn->crealm * 2 : 4;
                mn->realms = (PfRealmNode **)realloc(
                    mn->realms, mn->crealm * sizeof(PfRealmNode *));
                if (!mn->realms) pf_oom();
            }
            rn = (PfRealmNode *)malloc(sizeof(PfRealmNode));
            if (!rn) pf_oom();
            rn->name = e->realm;
            rn->accts = NULL;
            rn->nacct = rn->cacct = 0;
            map_init(&rn->ix);
            map_put(&mn->ix, e->realm, rn);
            mn->realms[mn->nrealm++] = rn;
        }
        if (!map_get(&rn->ix, e->acct)) {
            PfAcctNode *an;
            if (rn->nacct == rn->cacct) {
                rn->cacct = rn->cacct ? rn->cacct * 2 : 4;
                rn->accts = (PfAcctNode **)realloc(
                    rn->accts, rn->cacct * sizeof(PfAcctNode *));
                if (!rn->accts) pf_oom();
            }
            an = (PfAcctNode *)malloc(sizeof(PfAcctNode));
            if (!an) pf_oom();
            an->name = e->acct;
            map_put(&rn->ix, e->acct, an);
            rn->accts[rn->nacct++] = an;
        }
    }

    jw_obj_begin(out);
    jw_key(out, "sampled");
    jw_int(out, agg->total);
    jw_key(out, "sample_from");
    if (have_from) jw_double(out, from);
    else jw_null(out);
    jw_key(out, "sample_capped");
    jw_bool(out, (long long)ring->len >= ring->sample);
    jw_key(out, "success");
    jw_int(out, agg->ok);
    jw_key(out, "errors");
    jw_int(out, agg->err);
    jw_key(out, "client_aborted");
    jw_int(out, agg->aborted);
    jw_key(out, "success_rate_pct");
    if (agg->ok + agg->err) jw_double(out, pf_rate(agg->ok, agg->err));
    else jw_null(out);
    jw_key(out, "ttft_ms");
    pf_emit_block(out, &agg->ttfts);
    jw_key(out, "generation_ms");
    pf_emit_block(out, &agg->gens);
    jw_key(out, "wall_ms");
    pf_emit_block(out, &agg->walls);
    jw_key(out, "tokens_per_sec");
    pf_emit_block(out, &agg->tok_rates);
    jw_key(out, "cache_hit_pct");
    pf_emit_block(out, &agg->hits);
    jw_key(out, "by_model");
    jw_obj_begin(out);
    for (i = 0; i < nmodels; i++) {
        jw_key(out, models[i]->name);
        pf_emit_bucket(out, (PfBucket *)map_get(&agg->m, models[i]->name));
    }
    jw_obj_end(out);
    jw_key(out, "by_model_realm");
    jw_obj_begin(out);
    for (i = 0; i < nmodels; i++) {
        PfModelNode *mn = models[i];
        Map *rm = (Map *)map_get(&agg->mr, mn->name);
        jw_key(out, mn->name);
        jw_obj_begin(out);
        for (j = 0; j < mn->nrealm; j++) {
            jw_key(out, mn->realms[j]->name);
            pf_emit_bucket(out, rm ? (PfBucket *)map_get(rm, mn->realms[j]->name)
                                   : NULL);
        }
        jw_obj_end(out);
    }
    jw_obj_end(out);
    jw_key(out, "by_model_acct");
    jw_obj_begin(out);
    for (i = 0; i < nmodels; i++) {
        PfModelNode *mn = models[i];
        Map *rm = (Map *)map_get(&agg->ma, mn->name);
        jw_key(out, mn->name);
        jw_obj_begin(out);
        for (j = 0; j < mn->nrealm; j++) {
            PfRealmNode *rn = mn->realms[j];
            Map *am = rm ? (Map *)map_get(rm, rn->name) : NULL;
            jw_key(out, rn->name);
            jw_obj_begin(out);
            for (k = 0; k < rn->nacct; k++) {
                jw_key(out, rn->accts[k]->name);
                pf_emit_bucket(out, am ? (PfBucket *)map_get(am, rn->accts[k]->name)
                                       : NULL);
            }
            jw_obj_end(out);
        }
        jw_obj_end(out);
    }
    jw_obj_end(out);
    jw_obj_end(out);

    for (i = 0; i < nmodels; i++) {
        PfModelNode *mn = models[i];
        for (j = 0; j < mn->nrealm; j++) {
            PfRealmNode *rn = mn->realms[j];
            for (k = 0; k < rn->nacct; k++) free(rn->accts[k]);
            free(rn->accts);
            map_free(&rn->ix);
            free(rn);
        }
        free(mn->realms);
        map_free(&mn->ix);
        free(mn);
    }
    free(models);
    map_free(&model_ix);
}

/* ---- fold_perf --------------------------------------------------------- */

void fold_perf(const char *const *lines, const size_t *lens, size_t n,
               long long sample, const char *realm_filter,
               int has_since, double since, int has_until, double until,
               const Map *accounts, const char *default_realm, JW *out)
{
    PfAgg agg;
    PfRing ring;
    PfWin win;
    size_t i;

    /* 0 means no lower bound and 1e18 no upper bound (range_window's None
     * sides); 0 as a real bound is not expressible in Python either. */
    win.use_since = has_since && since != 0.0;
    win.since = since;
    win.use_until = has_until && until != 0.0 && until < 1e18;
    win.until = until;

    pf_agg_init(&agg);
    memset(&ring, 0, sizeof(ring));
    ring.sample = sample;
    if (sample > 0 && n > 0) {
        ring.cap = (sample < (long long)n) ? (size_t)sample : n;
        ring.ents = (PfEntry *)calloc(ring.cap, sizeof(PfEntry));
        if (!ring.ents) pf_oom();
    }
    /* sample <= 0 folds nothing at all, exactly like _perf_push's guard. */
    for (i = 0; i < n && sample > 0; i++) {
        if (lens[i] == 0) continue;   /* blank lines are not sample lines */
        pf_push_line(&ring, &agg, lines[i], lens[i], &win, realm_filter,
                     accounts, default_realm);
    }
    pf_payload(&agg, &ring, out);
    for (i = 0; i < ring.len; i++) pf_entry_clear(pf_ring_at(&ring, i));
    free(ring.ents);
    pf_agg_free(&agg);
}
