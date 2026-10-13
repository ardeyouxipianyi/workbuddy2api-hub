/* wbc_pricing.c — standalone C pricing engine (port of wb_pricing.py).
 *
 * Reference (read-only, the semantics' single source of truth):
 *   ../../native-full/wb_pricing.py — load_policies / load_timeline /
 *   policy_ref_at / compute_row / band_for / _band_applies / _utc_parts /
 *   _rate_for / _pick / _as_float / _doc_for / policy_entry / USD_CNY.
 *
 * Kept verbatim from the reference:
 *  - both price files are JSONL: blank lines and lines that do not parse are
 *    skipped; a policies row is keyed by its "id" (a later duplicate wins);
 *    a timeline row counts only when "policies" is an object, and the rows
 *    are sorted by "at" (stable) exactly as load_timeline() sorts them;
 *  - policy_ref_at: bisect_right(at) - 1 clamped to 0; that row's assignment
 *    wins (backfilled 0) even when it predates the stamp; else the first row
 *    at/after the stamp that lists the model — the whole timeline is scanned
 *    from the top, not from the bisect index (backfilled 1); else the nearest
 *    earlier row (backfilled 1); else no reference at all;
 *  - band_for: the *last* band whose condition holds wins; a model without
 *    bands, or a row matching none, falls back to the entry's flat table;
 *  - _band_applies: "min_prompt_tokens" (present and non-null, compared as a
 *    double) wins over the UTC window days + start <= HHMM < end (end
 *    defaults to 2400);
 *  - _rate_for: factor = usd_cny only when the currency is "USD"
 *    case-insensitively; a policy row defaults usd_cny to 7.1 (USD_CNY), a
 *    doc meta to 1.0, and a 0.0 rate collapses to 1.0 in both paths;
 *  - _pick: a missing/null rate falls back to its neighbour key, then to 0;
 *  - cost = (uncached*miss + cached*hit + completion*out) / unit * factor
 *    with cached = min(cached, prompt) and unit defaulting to 1e6 (0 too);
 *  - a row's own pid wins; a dangling pid still does not consult the timeline
 *    (only a row without one does); no policy -> the doc snapshot; no usable
 *    doc -> known=0 with source "builtin" (the C engine has no inline copy).
 *
 * Deliberate C-side divergences (contract level):
 *  - _as_float reads JSON numbers only (a numeric *string* reads as the
 *    default); real price files never carry those;
 *  - a missing/unreadable/broken doc file means "no doc" instead of Python's
 *    inline snapshot fallback;
 *  - only string policy ids / pids are usable (Python dicts could in theory
 *    be keyed by other scalars; the source never does that).
 *
 * Interface: the "wbc_pricing.h" section of CONTRACT.md (inlined below under
 * the guard the header itself would use -- this task is restricted to two
 * .c files; see the note above the guard for how to materialize the header).
 */
#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "wbc_json.h"
#include "wbc_util.h"

/* ---------------------------------------------------------------------
 * wbc_pricing.h is not written as a separate file (two-file limit on this
 * task).  The guarded block below IS the header text: to materialize
 * wbc_pricing.h, copy it verbatim into a file named wbc_pricing.h (main.c
 * and wbc_folds.h include it).
 * ------------------------------------------------------------------- */
#ifndef WBC_PRICING_H
#define WBC_PRICING_H

typedef struct Pricing Pricing;

/* policies/timeline 为 JSONL 路径；doc 为可选 pricing.json（内置快照替代品），
   可传 NULL 表示无文件版（则只认策略表/时间线，未命中即 known=0）。 */
Pricing *pricing_load(const char *policies_path, const char *timeline_path,
                      const char *doc_path);
void pricing_free(Pricing *);

typedef struct {
    int known;        /* 有价 */
    int disabled;     /* 总开关关（本引擎不产生；保留字段恒 0） */
    double cny;
    const char *source;      /* pid 或 "builtin" */
    int backfilled;
} Cost;

void pricing_cost(const Pricing *p, const char *model, double at,
                  double prompt, double cached, double completion,
                  const char *pid, Cost *out);

#endif /* WBC_PRICING_H */

/* ---- _as_float and truthiness ---------------------------------------- */

/* _as_float: a JSON number reads as its value, anything else as the default
 * (the contract's "非数→0"). */
static double num_or(const JVal *v, double dflt)
{
    if (v && jis_num(v)) return jnum(v);
    return dflt;
}

/* Python truthiness for the JSON shapes the price tables carry. */
static int val_truthy(const JVal *v)
{
    if (!v || jis_null(v)) return 0;
    if (jis_bool(v)) return jbool(v);
    if (jis_num(v)) return jnum(v) != 0.0;
    if (jis_str(v)) {
        const char *s = jstr(v);
        return s && s[0] != '\0';
    }
    return jlen(v) > 0;
}

static int ascii_ci_eq(const char *a, const char *b)
{
    for (;; a++, b++) {
        unsigned char ca = (unsigned char)*a;
        unsigned char cb = (unsigned char)*b;
        if (ca >= 'a' && ca <= 'z') ca = (unsigned char)(ca - ('a' - 'A'));
        if (cb >= 'a' && cb <= 'z') cb = (unsigned char)(cb - ('a' - 'A'));
        if (ca != cb) return 0;
        if (!ca) return 1;
    }
}

/* ---- _utc_parts ------------------------------------------------------- */

static const char *const WBC_WEEKDAYS[7] = {
    "mon", "tue", "wed", "thu", "fri", "sat", "sun"
};

/* Python time.gmtime: 1970-01-01 was a Thursday, so day index 0 maps to
 * "thu".  A non-finite stamp reads as 0 (gmtime(0)). */
static void utc_parts(double at, const char **day_out, int *hhmm_out)
{
    double secs, days, in_day, dow;
    int hour, minute;
    if (!isfinite(at)) at = 0.0;
    secs = floor(at);
    days = floor(secs / 86400.0);
    in_day = secs - days * 86400.0;
    dow = fmod(fmod(days, 7.0) + 10.0, 7.0);  /* +3 for Thursday, +7 keeps >= 0 */
    hour = (int)(in_day / 3600.0);
    minute = (int)((in_day - (double)hour * 3600.0) / 60.0);
    *day_out = WBC_WEEKDAYS[(int)dow];
    *hhmm_out = hour * 100 + minute;
}

/* ---- band_for / _band_applies ----------------------------------------- */

/* Python `day in days` for the shapes the source uses: a list of weekday
 * names, or (degenerately) a string, where `in` is a substring test. */
static int days_has(const JVal *days, const char *day)
{
    size_t i, n;
    if (jis_str(days)) {
        const char *s = jstr(days);
        return s && strstr(s, day) != NULL;
    }
    if (!jis_arr(days)) return 0;
    n = jlen(days);
    for (i = 0; i < n; i++) {
        const JVal *e = jat(days, i);
        if (e && jis_str(e)) {
            const char *s = jstr(e);
            if (s && strcmp(s, day) == 0) return 1;
        }
    }
    return 0;
}

/* _band_applies: a length threshold (present and non-null) wins, else the
 * UTC window start <= HHMM < end with end defaulting to 2400. */
static int band_applies(const JVal *band, double prompt, const char *day, int hhmm)
{
    const JVal *thr = jget(band, "min_prompt_tokens");
    const JVal *days;
    double start, end;
    if (thr && !jis_null(thr))
        return prompt >= num_or(thr, 0.0);
    days = jget(band, "days");
    if (days && val_truthy(days) && !days_has(days, day))
        return 0;
    start = num_or(jget(band, "start"), 0.0);
    end = num_or(jget(band, "end"), 0.0);
    if (end == 0.0) end = 2400.0;   /* Python `_as_float(end) or 2400.0` */
    return start <= (double)hhmm && (double)hhmm < end;
}

/* band_for: the *last* band that applies and carries an object flat wins;
 * with no bands (or no match) the entry's own flat is used.  NULL means
 * "no usable table" — every rate then reads 0, like Python's {} fallback. */
static const JVal *flat_for(const JVal *entry, double at, double prompt)
{
    const JVal *bands = jget(entry, "bands");
    const JVal *flat;
    if (bands && jis_arr(bands) && jlen(bands) > 0) {
        const char *day;
        const JVal *hit = NULL;
        int hhmm, i, n = (int)jlen(bands);
        utc_parts(at, &day, &hhmm);
        for (i = 0; i < n; i++) {
            const JVal *band = jat(bands, (size_t)i);
            const JVal *tbl;
            if (!band || !jis_obj(band)) continue;
            if (!band_applies(band, prompt, day, hhmm)) continue;
            tbl = jget(band, "flat");
            if (tbl && jis_obj(tbl)) hit = tbl;
        }
        if (hit) return hit;
    }
    flat = jget(entry, "flat");
    if (flat && jis_obj(flat)) return flat;
    return NULL;
}

/* _pick: a missing/null rate falls back to its neighbour key, then reads as
 * a number (non-number -> 0). */
static double rate_pick(const JVal *rate, const char *key, const char *fallback)
{
    const JVal *v;
    if (!rate) return 0.0;
    v = jget(rate, key);
    if (v && jis_null(v)) v = NULL;
    if (!v && fallback) {
        v = jget(rate, fallback);
        if (v && jis_null(v)) v = NULL;
    }
    return num_or(v, 0.0);
}

/* compute_row's tail: rates -> CNY.  `entry` is a policy row or a doc model
 * entry — flat / bands / unit read identically from both. */
static double compute_cny(const JVal *entry, double at, double prompt,
                          double cached, double completion, double factor)
{
    const JVal *rate = flat_for(entry, at, prompt);
    double unit = num_or(jget(entry, "unit"), 0.0);
    double c, uncached, miss, hit, out_rate;
    if (unit == 0.0) unit = 1000000.0;           /* Python `or 1000000.0` */
    c = (prompt < cached) ? prompt : cached;     /* min(cached, prompt) */
    uncached = prompt - c;
    miss = rate_pick(rate, "input_cache_miss", NULL);
    hit = rate_pick(rate, "input_cache_hit", "input_cache_miss");
    out_rate = rate_pick(rate, "output", NULL);
    return (uncached * miss + c * hit + completion * out_rate) / unit * factor;
}

/* _doc_for + _rate_for for a policy row: currency defaults to "USD";
 * usd_cny defaults to USD_CNY (7.1) and a 0 rate collapses to 1.0. */
static double policy_factor(const JVal *policy)
{
    const JVal *c = jget(policy, "currency");
    double v;
    int usd;
    if (!c || !val_truthy(c)) usd = 1;               /* `policy.get(...) or "USD"` */
    else if (jis_str(c)) usd = ascii_ci_eq(jstr(c), "USD");
    else usd = 0;                                    /* str(non-string) != "USD" */
    if (!usd) return 1.0;
    v = num_or(jget(policy, "usd_cny"), 7.1);        /* USD_CNY */
    return (v != 0.0) ? v : 1.0;                     /* `_as_float(...) or 1.0` */
}

/* _rate_for for a doc entry: currency defaults to "" (no conversion);
 * meta.usd_cny defaults to 1.0 and 0 collapses to 1.0. */
static double doc_factor(const JVal *entry, const JVal *meta)
{
    const JVal *c = jget(entry, "currency");
    const JVal *m;
    double v;
    if (!c || !jis_str(c) || !ascii_ci_eq(jstr(c), "USD")) return 1.0;
    m = (meta && jis_obj(meta)) ? jget(meta, "usd_cny") : NULL;
    v = num_or(m, 1.0);
    return (v != 0.0) ? v : 1.0;
}

/* ---- file reading ----------------------------------------------------- */

/* Whole file into a NUL-terminated buffer; NULL when it cannot be read. */
static char *read_whole_file(const char *path, size_t *len_out)
{
    FILE *fh;
    char *buf = NULL;
    size_t cap = 0, len = 0;
    if (!path) return NULL;
    fh = fopen(path, "rb");
    if (!fh) return NULL;
    for (;;) {
        size_t got;
        if (len + 4097 > cap) {
            size_t ncap = cap ? cap : 8192;
            char *nb;
            while (ncap < len + 4097) ncap *= 2;
            nb = (char *)realloc(buf, ncap);
            if (!nb) {
                free(buf);
                fclose(fh);
                fprintf(stderr, "wb-agg: out of memory\n");
                exit(3);
            }
            buf = nb;
            cap = ncap;
        }
        got = fread(buf + len, 1, 4096, fh);
        len += got;
        if (got < 4096) break;
    }
    fclose(fh);
    if (!buf) {
        buf = (char *)malloc(1);
        if (!buf) {
            fprintf(stderr, "wb-agg: out of memory\n");
            exit(3);
        }
    }
    buf[len] = '\0';
    if (len_out) *len_out = len;
    return buf;
}

typedef struct {
    void **items;
    size_t n, cap;
} PtrArr;

static void ptrarr_push(PtrArr *a, void *p)
{
    if (a->n == a->cap) {
        size_t ncap = a->cap ? a->cap * 2 : 16;
        void **ni = (void **)realloc(a->items, ncap * sizeof(void *));
        if (!ni) {
            fprintf(stderr, "wb-agg: out of memory\n");
            exit(3);
        }
        a->items = ni;
        a->cap = ncap;
    }
    a->items[a->n++] = p;
}

/* _read_jsonl: read the file, strip each line, skip blanks and lines that
 * do not parse; objects are collected into `rows` (all parsed trees land in
 * `roots` for the caller to free).  A missing/unreadable file yields none. */
static void read_jsonl(const char *path, PtrArr *roots, PtrArr *rows)
{
    char *buf;
    size_t len = 0, pos = 0;
    buf = read_whole_file(path, &len);
    if (!buf) return;
    while (pos < len) {
        size_t start = pos, end;
        JVal *v;
        while (pos < len && buf[pos] != '\n') pos++;
        end = pos;
        if (pos < len) pos++;                    /* consume the newline */
        while (start < end && isspace((unsigned char)buf[start])) start++;
        while (end > start && isspace((unsigned char)buf[end - 1])) end--;
        if (start == end) continue;
        v = jparse(buf + start, end - start);
        if (!v) continue;
        ptrarr_push(roots, v);
        if (jis_obj(v)) ptrarr_push(rows, v);
    }
    free(buf);
}

/* ---- timeline ordering ------------------------------------------------ */

/* load_timeline sorts by _as_float("at") with a stable sort; sorting the
 * (at, sequence) pair reproduces it exactly for any input order. */
typedef struct {
    double at;
    size_t seq;
    JVal *row;
} TlRow;

static int tl_cmp(const void *a, const void *b)
{
    const TlRow *x = (const TlRow *)a;
    const TlRow *y = (const TlRow *)b;
    if (x->at < y->at) return -1;
    if (x->at > y->at) return 1;
    if (x->seq < y->seq) return -1;
    if (x->seq > y->seq) return 1;
    return 0;
}

/* ---- the pricing state ------------------------------------------------ */

struct Pricing {
    Map policies;        /* pid -> JVal* policy row */
    JVal **timeline;     /* rows carrying an object "policies", sorted by at */
    double *timeline_ats;
    size_t timeline_n;
    JVal *doc;           /* parsed pricing.json; NULL when absent/unusable */
    PtrArr roots;        /* every parsed tree, freed by pricing_free */
};

Pricing *pricing_load(const char *policies_path, const char *timeline_path,
                      const char *doc_path)
{
    Pricing *p;
    size_t i;
    p = (Pricing *)calloc(1, sizeof(*p));
    if (!p) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    map_init(&p->policies);

    /* load_policies: {id: row}; a falsy or non-string id is not a key. */
    {
        PtrArr rows;
        rows.items = NULL;
        rows.n = rows.cap = 0;
        read_jsonl(policies_path, &p->roots, &rows);
        for (i = 0; i < rows.n; i++) {
            const JVal *id = jget(rows.items[i], "id");
            if (id && jis_str(id)) {
                const char *s = jstr(id);
                if (s && s[0]) map_put(&p->policies, s, rows.items[i]);
            }
        }
        free(rows.items);
    }

    /* load_timeline: rows with an object "policies", sorted by "at".
     * Python sorts with a stable sort; the (at, seq) key reproduces it. */
    {
        PtrArr rows;
        TlRow *tmp;
        rows.items = NULL;
        rows.n = rows.cap = 0;
        read_jsonl(timeline_path, &p->roots, &rows);
        {
            size_t keep = 0;
            for (i = 0; i < rows.n; i++) {
                const JVal *pol = jget(rows.items[i], "policies");
                if (pol && jis_obj(pol)) rows.items[keep++] = rows.items[i];
            }
            rows.n = keep;
        }
        if (rows.n) {
            tmp = (TlRow *)malloc(rows.n * sizeof(*tmp));
            p->timeline = (JVal **)malloc(rows.n * sizeof(*p->timeline));
            p->timeline_ats = (double *)malloc(rows.n * sizeof(*p->timeline_ats));
            if (!tmp || !p->timeline || !p->timeline_ats) {
                fprintf(stderr, "wb-agg: out of memory\n");
                exit(3);
            }
            for (i = 0; i < rows.n; i++) {
                tmp[i].at = num_or(jget(rows.items[i], "at"), 0.0);
                tmp[i].seq = i;
                tmp[i].row = (JVal *)rows.items[i];
            }
            qsort(tmp, rows.n, sizeof(*tmp), tl_cmp);
            for (i = 0; i < rows.n; i++) {
                p->timeline[i] = tmp[i].row;
                p->timeline_ats[i] = tmp[i].at;
            }
            free(tmp);
            p->timeline_n = rows.n;
        }
        free(rows.items);
    }

    /* The optional snapshot.  Python falls back to its inline copy on a
     * broken/missing file; the C engine has none, so "no usable doc" means an
     * unpriced model stays known=0. */
    if (doc_path) {
        size_t len = 0;
        char *buf = read_whole_file(doc_path, &len);
        if (buf) {
            JVal *v = jparse(buf, len);
            free(buf);
            if (v) {
                ptrarr_push(&p->roots, v);
                if (jis_obj(v)) {
                    const JVal *models = jget(v, "models");
                    if (models && jis_obj(models)) p->doc = v;
                }
            }
        }
    }
    return p;
}

void pricing_free(Pricing *p)
{
    size_t i;
    if (!p) return;
    map_free(&p->policies);
    for (i = 0; i < p->roots.n; i++) jfree(p->roots.items[i]);
    free(p->roots.items);
    free(p->timeline);
    free(p->timeline_ats);
    free(p);
}

/* ---- policy_ref_at ---------------------------------------------------- */

/* policy_ref_at: (pid, backfilled) for a moment in the past.  A returned pid
 * points into the timeline tree and stays valid while `p` lives. */
static void policy_ref_at(const Pricing *p, double at, const char *model,
                          const char **pid_out, int *backfilled_out)
{
    size_t lo, hi, n, i;
    long long index;
    *pid_out = NULL;
    *backfilled_out = 0;
    n = p->timeline_n;
    if (!n) return;

    /* bisect_right(timeline_ats, stamp) - 1, clamped to 0 (Python's loop). */
    lo = 0;
    hi = n;
    while (lo < hi) {
        size_t mid = lo + (hi - lo) / 2;
        if (at < p->timeline_ats[mid]) hi = mid;
        else lo = mid + 1;
    }
    index = (long long)lo - 1;
    if (index < 0) index = 0;

    /* The row at/just before the stamp: a membership test, not a truth test. */
    {
        const JVal *pol = jget(p->timeline[index], "policies");
        const JVal *v = (model && pol && jis_obj(pol)) ? jget(pol, model) : NULL;
        if (v) {
            const char *s = jis_str(v) ? jstr(v) : NULL;
            if (s && s[0]) *pid_out = s;
            return;   /* unusable value: doc path, like Python's falsy pid */
        }
    }

    /* The first *later* row that lists the model — Python scans the whole
     * timeline from the top here, not from the bisect index. */
    for (i = 0; i < n; i++) {
        const JVal *pol, *v;
        if (p->timeline_ats[i] < at) continue;
        pol = jget(p->timeline[i], "policies");
        v = (model && pol && jis_obj(pol)) ? jget(pol, model) : NULL;
        if (v && val_truthy(v)) {
            const char *s = jis_str(v) ? jstr(v) : NULL;
            if (s && s[0]) *pid_out = s;
            *backfilled_out = 1;
            return;
        }
    }

    /* Failing that, the nearest earlier row: reversed(timeline[:index+1]). */
    for (i = (size_t)index + 1; i-- > 0; ) {
        const JVal *pol, *v;
        pol = jget(p->timeline[i], "policies");
        v = (model && pol && jis_obj(pol)) ? jget(pol, model) : NULL;
        if (v && val_truthy(v)) {
            const char *s = jis_str(v) ? jstr(v) : NULL;
            if (s && s[0]) *pid_out = s;
            *backfilled_out = 1;
            return;
        }
    }
}

/* ---- pricing_cost ----------------------------------------------------- */

void pricing_cost(const Pricing *p, const char *model, double at,
                  double prompt, double cached, double completion,
                  const char *pid, Cost *out)
{
    const JVal *policy;
    if (!out) return;
    out->known = 0;
    out->disabled = 0;
    out->cny = 0.0;
    out->source = "builtin";
    out->backfilled = 0;
    if (!p) return;

    /* The row's own reference wins; only a row without one consults the
     * timeline, and a dangling pid does not fall back to it. */
    if (!pid || !pid[0]) policy_ref_at(p, at, model, &pid, &out->backfilled);
    policy = pid ? (const JVal *)map_get(&p->policies, pid) : NULL;
    if (policy) {
        out->cny = compute_cny(policy, at, prompt, cached, completion,
                               policy_factor(policy));
        out->known = 1;
        out->source = pid;
        return;
    }

    /* No policy: the snapshot's model entry, else unpriced. */
    out->backfilled = 0;
    if (p->doc && model) {
        const JVal *models = jget(p->doc, "models");
        const JVal *entry = (models && jis_obj(models)) ? jget(models, model) : NULL;
        if (entry && jis_obj(entry)) {
            out->cny = compute_cny(entry, at, prompt, cached, completion,
                                   doc_factor(entry, jget(p->doc, "meta")));
            out->known = 1;
            out->source = "builtin";
        }
    }
}
