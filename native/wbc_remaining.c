/* wbc_remaining.c -- remaining-usage fold for the standalone engine.
 *
 * Port of wb_proxy's remaining module per PERF_REMAINING_SPEC.md section 二/三:
 * _pair_buffer/_pair_append/_pair_trim/_range_sum/_range_sum_open, _merge_event,
 * _fold_remaining, _budget_means, _remaining_budgets, _remaining_payload_locked
 * and parse_rate_limit_reset.
 *
 * The fold reads each usage row's parsed JSON tree directly (main hands the
 * tree to remaining_fold_json before freeing it): the row carries
 * "status"/"message", which the Row struct does not keep, and the spec pins
 * this contract.  C99, no external dependencies; allocation failure exits(3).
 *
 * accounts map: the Map's values are `struct rm_account *` (layout repeated
 * below, main defines the same struct).  model_caps is a JVal object:
 * model -> {"at": ..., "reset": ...}.
 */
#define _POSIX_C_SOURCE 200809L

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "wbc_json.h"
#include "wbc_util.h"

#if defined(_WIN32)
#  define RM_STD_OFFSET() (_timezone)   /* declared by <time.h> */
#else
extern long timezone;                   /* POSIX global: west positive */
#  define RM_STD_OFFSET() (timezone)
#endif

#define RM_WINDOW_SECONDS 86400.0       /* LIMIT_WINDOW_SECONDS */
#define RM_KEEP_SECONDS 93600.0         /* _REMAINING_KEEP_SECONDS (26h) */
#define RM_MAX_EVENTS 1000u             /* _REMAINING_MAX_EVENTS */
#define RM_TRIM_COMPACT 512u            /* _pair_trim's compaction threshold */

#define RM_SRC_LOG 0
#define RM_SRC_FILE 1

#if defined(__GNUC__)
#  define RM_MAYBE_UNUSED __attribute__((unused))
#else
#  define RM_MAYBE_UNUSED
#endif

/* ---- state shapes ---- */

typedef struct Remaining Remaining;

/* Mirrors main's account record (same fields, same order). */
struct rm_account {
    const char *uid;
    const char *nickname;
    const char *realm;
    int enabled;
    const JVal *model_caps;
};

/* One (uid, model) pair: ats ascending, cum absolute prefix sums, head the
 * first live index, base the absolute cum of everything trimmed away. */
typedef struct {
    char *uid;
    char *model;
    double *ats;
    long long *cum;
    size_t len, cap;
    size_t head;
    long long base;
} RmPair;

typedef struct {
    double at;
    double reset;
    char *account;
    char *model;
    char *realm;
    long long sample;
    int src;
} RmEvent;

struct Remaining {
    Map usage;              /* "len:uidmodel" -> RmPair* */
    RmEvent *events;
    size_t n_events;
    size_t cap_events;
};

/* ---- small helpers ---- */

static void *rm_alloc(size_t n)
{
    void *p = malloc(n ? n : 1);
    if (!p) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    return p;
}

static void *rm_realloc(void *p, size_t n)
{
    void *q = realloc(p, n ? n : 1);
    if (!q) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    return q;
}

static void *rm_calloc(size_t n, size_t sz)
{
    void *p = calloc(n ? n : 1, sz);
    if (!p) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    return p;
}

static char *rm_strdup(const char *s)
{
    size_t n = strlen(s) + 1;
    char *out = (char *)rm_alloc(n);
    memcpy(out, s, n);
    return out;
}

static uint64_t rm_bits(double d)
{
    uint64_t u;
    memcpy(&u, &d, sizeof u);
    return u;
}

/* Python truthiness for the value shapes a usage row carries. */
static int rm_truthy(const JVal *v)
{
    if (!v || jis_null(v)) return 0;
    if (jis_bool(v)) return jbool(v);
    if (jis_num(v)) return jnum(v) != 0.0;
    if (jis_str(v)) {
        const char *s = jstr(v);
        return s && s[0] != '\0';
    }
    if (jis_obj(v)) return jobj_len(v) > 0;
    return jlen(v) > 0;
}

static int rm_isdigit(char c)
{
    return c >= '0' && c <= '9';
}

static int rm_isspace_c(char c)
{
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v'
        || c == '\f';
}

/* float(str): the whole string must parse (trailing blanks allowed), like
 * Python float().  Underscore separators are not accepted (never in logs). */
static int rm_str_to_double(const char *s, double *out)
{
    char *end;
    double d;
    while (rm_isspace_c(*s)) s++;
    if (!*s) return 0;
    d = strtod(s, &end);
    if (end == s) return 0;
    while (rm_isspace_c(*end)) end++;
    if (*end) return 0;
    *out = d;
    return 1;
}

/* float(x or 0); returns 0 when Python would raise (caller skips). */
static int rm_to_float(const JVal *v, double *out)
{
    if (!rm_truthy(v)) {
        *out = 0.0;
        return 1;
    }
    if (jis_bool(v)) {
        *out = jbool(v) ? 1.0 : 0.0;
        return 1;
    }
    if (jis_num(v)) {
        *out = jnum(v);
        return 1;
    }
    if (jis_str(v)) return rm_str_to_double(jstr(v), out);
    return 0;
}

/* int(x or 0): int64 truncation toward zero; strings parse as integers. */
static int rm_as_ll(const JVal *v, long long *out)
{
    if (!rm_truthy(v)) {
        *out = 0;
        return 1;
    }
    if (jis_bool(v)) {
        *out = jbool(v) ? 1 : 0;
        return 1;
    }
    if (jis_num(v)) {
        double d;
        if (jnum_is_int(v)) {
            *out = jint(v);
            return 1;
        }
        d = jnum(v);
        if (d != d || d >= 9223372036854775808.0 || d < -9223372036854775808.0)
            return 0;
        *out = (long long)d;
        return 1;
    }
    if (jis_str(v)) {
        const char *s = jstr(v);
        char *end;
        long long r;
        while (rm_isspace_c(*s)) s++;
        if (!*s) return 0;
        r = strtoll(s, &end, 10);
        if (end == s) return 0;
        while (rm_isspace_c(*end)) end++;
        if (*end) return 0;             /* int("1.5") raises in Python */
        *out = r;
        return 1;
    }
    return 0;
}

/* str(x or ""): strings verbatim, ints exactly, doubles/floats via %.17g
 * (Python repr differs only for crafted non-string ids; documented). */
static void rm_to_text(Buf *out, const JVal *v)
{
    buf_puts(out, "");
    if (!rm_truthy(v)) return;
    if (jis_str(v)) {
        buf_puts(out, jstr(v));
        return;
    }
    if (jis_bool(v)) {
        buf_puts(out, jbool(v) ? "True" : "False");
        return;
    }
    if (jis_num(v)) {
        if (jnum_is_int(v)) buf_printf(out, "%lld", jint(v));
        else buf_printf(out, "%.17g", jnum(v));
        return;
    }
    /* other truthy shapes: Python str() would give a repr; treated empty. */
}

/* "len(a):a b" -- injective for the (a, b) pairs the maps are keyed on. */
static void rm_key_build(Buf *b, const char *a, const char *c)
{
    b->len = 0;
    buf_printf(b, "%zu:%s%s", strlen(a), a, c);
}

/* ---- pair buffers (_pair_buffer/_pair_append/_pair_trim) ---- */

static size_t rm_lower_bound(const double *a, double x, size_t lo, size_t hi)
{
    while (lo < hi) {
        size_t mid = lo + (hi - lo) / 2;
        if (a[mid] < x) lo = mid + 1;
        else hi = mid;
    }
    return lo;
}

static size_t rm_upper_bound(const double *a, double x, size_t lo, size_t hi)
{
    while (lo < hi) {
        size_t mid = lo + (hi - lo) / 2;
        if (x < a[mid]) hi = mid;
        else lo = mid + 1;
    }
    return lo;
}

static void rm_pair_grow(RmPair *p, size_t need)
{
    size_t cap;
    if (need <= p->cap) return;
    cap = p->cap ? p->cap : 16;
    while (cap < need) cap *= 2;
    p->ats = (double *)rm_realloc(p->ats, cap * sizeof(double));
    p->cum = (long long *)rm_realloc(p->cum, cap * sizeof(long long));
    p->cap = cap;
}

static void rm_pair_drop_prefix(RmPair *p, size_t count)
{
    if (count == 0) return;
    memmove(p->ats, p->ats + count, (p->len - count) * sizeof(double));
    memmove(p->cum, p->cum + count, (p->len - count) * sizeof(long long));
    p->len -= count;
}

static void rm_pair_append(RmPair *p, double at, long long tokens)
{
    size_t index, i;
    if (p->len == 0 || at >= p->ats[p->len - 1]) {
        rm_pair_grow(p, p->len + 1);
        p->ats[p->len] = at;
        p->cum[p->len] = (p->len > 0 ? p->cum[p->len - 1] : 0) + tokens;
        p->len++;
        return;
    }
    index = rm_upper_bound(p->ats, at, 0, p->len);
    if (index < p->head) {
        /* The sample belongs before the dead prefix: compact it away first
         * (same absolute-base rule as the trim), because the freshly folded
         * row is live by definition and must not land behind `head`. */
        p->base = p->cum[p->head - 1];
        rm_pair_drop_prefix(p, p->head);
        p->head = 0;
        index = rm_upper_bound(p->ats, at, 0, p->len);
    }
    rm_pair_grow(p, p->len + 1);
    memmove(&p->ats[index + 1], &p->ats[index],
            (p->len - index) * sizeof(double));
    memmove(&p->cum[index + 1], &p->cum[index],
            (p->len - index) * sizeof(long long));
    p->ats[index] = at;
    p->cum[index] = (index > 0 ? p->cum[index - 1] : p->base) + tokens;
    for (i = index + 1; i <= p->len; i++) p->cum[i] += tokens;
    p->len++;
}

static void rm_pair_trim(RmPair *p, double cutoff)
{
    while (p->head < p->len && p->ats[p->head] < cutoff) p->head++;
    if (p->head > RM_TRIM_COMPACT && p->head * 2 > p->len) {
        /* base is the ABSOLUTE prefix sum of the trimmed prefix (cum counts
         * from the very first buffered row and is never reset by a slice);
         * += here would double-count the prefix on the next compaction. */
        p->base = p->cum[p->head - 1];
        rm_pair_drop_prefix(p, p->head);
        p->head = 0;
    }
}

/* _range_sum: lo <= at <= hi. */
static long long rm_range_sum(const RmPair *p, double lo, double hi)
{
    size_t j, k;
    long long low;
    if (!p || p->head >= p->len) return 0;
    j = rm_lower_bound(p->ats, lo, p->head, p->len);
    k = rm_upper_bound(p->ats, hi, p->head, p->len);
    if (k <= j) return 0;
    low = j > 0 ? p->cum[j - 1] : p->base;
    return p->cum[k - 1] - low;
}

/* _range_sum_open: lo <= at < hi (kept for the archive-day口径; the payload
 * itself only sums closed ranges). */
static RM_MAYBE_UNUSED long long rm_range_sum_open(const RmPair *p, double lo,
                                                   double hi)
{
    size_t j, k;
    long long low;
    if (!p || p->head >= p->len) return 0;
    j = rm_lower_bound(p->ats, lo, p->head, p->len);
    k = rm_lower_bound(p->ats, hi, p->head, p->len);
    if (k <= j) return 0;
    low = j > 0 ? p->cum[j - 1] : p->base;
    return p->cum[k - 1] - low;
}

static RmPair *rm_pair_find(const Remaining *st, const char *uid,
                            const char *model)
{
    Buf key;
    RmPair *p;
    buf_init(&key);
    rm_key_build(&key, uid, model);
    p = (RmPair *)map_get(&st->usage, key.data);
    buf_free(&key);
    return p;
}

static RmPair *rm_pair_get_or_new(Remaining *st, const char *uid,
                                  const char *model)
{
    Buf key;
    RmPair *p;
    buf_init(&key);
    rm_key_build(&key, uid, model);
    p = (RmPair *)map_get(&st->usage, key.data);
    if (!p) {
        p = (RmPair *)rm_calloc(1, sizeof *p);
        p->uid = rm_strdup(uid);
        p->model = rm_strdup(model);
        map_put(&st->usage, key.data, p);
    }
    buf_free(&key);
    return p;
}

/* ---- cap anchors (_cap_anchor/_cap_anchors/_cap_window_start) ---- */

typedef struct {
    double at;
    double reset;
} RmAnchor;

typedef struct {
    RmAnchor *v;
    size_t n, cap;
} RmAnchorList;

static void rm_anchor_list_init(RmAnchorList *l)
{
    l->v = NULL;
    l->n = l->cap = 0;
}

static void rm_anchor_list_free(RmAnchorList *l)
{
    free(l->v);
    l->v = NULL;
    l->n = l->cap = 0;
}

/* Deduplicated by reset (bit pattern == float equality for every value that
 * passes reset <= 0; NaN passes the guard and never matches, like Python). */
static void rm_anchor_add(RmAnchorList *l, double at, double reset)
{
    size_t i;
    if (reset <= 0) return;
    if (!isnan(reset)) {
        for (i = 0; i < l->n; i++) {
            if (rm_bits(l->v[i].reset) == rm_bits(reset)) {
                if (at > l->v[i].at) l->v[i].at = at;
                return;
            }
        }
    }
    if (l->n == l->cap) {
        l->cap = l->cap ? l->cap * 2 : 8;
        l->v = (RmAnchor *)rm_realloc(l->v, l->cap * sizeof(RmAnchor));
    }
    l->v[l->n].at = at;
    l->v[l->n].reset = reset;
    l->n++;
}

static void rm_collect_anchors(const Remaining *st, const char *uid,
                               const char *model, RmAnchorList *l)
{
    size_t i;
    for (i = 0; i < st->n_events; i++) {
        const RmEvent *e = &st->events[i];
        if (strcmp(e->account, uid) == 0 && strcmp(e->model, model) == 0)
            rm_anchor_add(l, e->at, e->reset);
    }
}

/* prev = largest other reset < reset (0 floor); max(prev, reset - 24h). */
static double rm_cap_window_start(const RmAnchorList *l, double reset)
{
    double prev = 0.0;
    double alt = reset - RM_WINDOW_SECONDS;
    size_t i;
    for (i = 0; i < l->n; i++) {
        double other = l->v[i].reset;
        if (other < reset && other > prev) prev = other;
    }
    return alt > prev ? alt : prev;
}

/* ---- event table (_merge_event) ---- */

static void rm_merge_event(Remaining *st, double at, double reset,
                           const char *account, const char *model,
                           const char *realm, long long sample, int src)
{
    size_t i, drop;
    RmEvent *ev;
    if (!account || !account[0] || !model || !model[0] || reset <= 0) return;
    if (!isnan(reset)) {
        for (i = 0; i < st->n_events; i++) {
            RmEvent *e = &st->events[i];
            if (rm_bits(e->reset) == rm_bits(reset)
                && strcmp(e->account, account) == 0
                && strcmp(e->model, model) == 0)
                return;
        }
    }
    if (st->n_events == st->cap_events) {
        st->cap_events = st->cap_events ? st->cap_events * 2 : 32;
        st->events = (RmEvent *)rm_realloc(st->events,
                                           st->cap_events * sizeof(RmEvent));
    }
    ev = &st->events[st->n_events++];
    ev->at = at;
    ev->reset = reset;
    ev->account = rm_strdup(account);
    ev->model = rm_strdup(model);
    ev->realm = rm_strdup(realm ? realm : "");
    ev->sample = sample;
    ev->src = src;
    if (st->n_events > RM_MAX_EVENTS) {
        /* Drop from the head and forget those dedup keys in the same move
         * (the keys live in the table itself, so they leave with it). */
        drop = st->n_events - RM_MAX_EVENTS;
        for (i = 0; i < drop; i++) {
            free(st->events[i].account);
            free(st->events[i].model);
            free(st->events[i].realm);
        }
        memmove(st->events, st->events + drop,
                (st->n_events - drop) * sizeof(RmEvent));
        st->n_events -= drop;
    }
}

/* ---- parse_rate_limit_reset ---- */

/* (\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}) with Python strptime's ranges.
 * 1 = parsed, 0 = the shape does not match, -1 = shape matched but a range is
 * invalid (strptime would raise -> the whole parse returns None). */
static int rm_parse_stamp(const char *s, size_t avail, struct tm *out)
{
    int y, mo, d, h, mi, se, i;
    if (avail < 19) return 0;
    for (i = 0; i < 19; i++) {
        char c = s[i];
        if (i == 4 || i == 7) {
            if (c != '-') return 0;
            continue;
        }
        if (i == 10) {
            if (c != ' ' && c != 'T') return 0;
            continue;
        }
        if (i == 13 || i == 16) {
            if (c != ':') return 0;
            continue;
        }
        if (!rm_isdigit(c)) return 0;
    }
    y = (s[0] - '0') * 1000 + (s[1] - '0') * 100 + (s[2] - '0') * 10
        + (s[3] - '0');
    mo = (s[5] - '0') * 10 + (s[6] - '0');
    d = (s[8] - '0') * 10 + (s[9] - '0');
    h = (s[11] - '0') * 10 + (s[12] - '0');
    mi = (s[14] - '0') * 10 + (s[15] - '0');
    se = (s[17] - '0') * 10 + (s[18] - '0');
    if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || se > 61)
        return -1;                      /* strptime would raise ValueError */
    memset(out, 0, sizeof *out);
    out->tm_year = y - 1900;
    out->tm_mon = mo - 1;
    out->tm_mday = d;
    out->tm_hour = h;
    out->tm_min = mi;
    out->tm_sec = se;
    out->tm_isdst = -1;
    return 1;
}

/* Upstream 429 body -> reset epoch, or 0 for Python's None.
 * First (?:reset at|将在)\s*<stamp> match, then the first UTC([+-]\d{1,2})
 * (?::?(\d{2}))? in the whole string; base = mktime(local stamp) - timezone;
 * result = base - offset. */
static int rm_parse_rate_limit_reset(const char *detail, double *out)
{
    static const char cn[] = "\xe5\xb0\x86\xe5\x9c\xa8";   /* 将在 */
    struct tm stamp;
    size_t n, i, p;
    int found = 0;
    int st;
    double offset = 0.0;
    time_t base;
    time_t probe;

    if (!detail || !detail[0]) return 0;
    n = strlen(detail);
    for (i = 0; i < n; i++) {
        p = i;
        if (n - p >= 8 && strncmp(detail + p, "reset at", 8) == 0) p += 8;
        else if (n - p >= 6 && strncmp(detail + p, cn, 6) == 0) p += 6;
        else continue;
        while (p < n && rm_isspace_c(detail[p])) p++;
        if (p >= n) continue;
        st = rm_parse_stamp(detail + p, n - p, &stamp);
        if (st == 1) {
            found = 1;
            break;
        }
        if (st < 0) return 0;           /* committed match, invalid stamp */
    }
    if (!found) return 0;
    for (i = 0; n - i >= 5; i++) {
        int sign, hours, minutes = 0, has_min = 0;
        size_t q;
        if (strncmp(detail + i, "UTC", 3) != 0) continue;
        q = i + 3;
        if (detail[q] != '+' && detail[q] != '-') continue;
        sign = detail[q] == '-' ? -1 : 1;
        q++;
        if (!rm_isdigit(detail[q])) continue;
        hours = detail[q] - '0';
        q++;
        if (q < n && rm_isdigit(detail[q])) {
            hours = hours * 10 + (detail[q] - '0');
            q++;
        }
        if (q < n && detail[q] == ':' && q + 2 < n && rm_isdigit(detail[q + 1])
            && rm_isdigit(detail[q + 2])) {
            minutes = (detail[q + 1] - '0') * 10 + (detail[q + 2] - '0');
            has_min = 1;
        } else if (q + 1 < n && rm_isdigit(detail[q])
                   && rm_isdigit(detail[q + 1])) {
            minutes = (detail[q] - '0') * 10 + (detail[q + 1] - '0');
            has_min = 1;
        }
        if (has_min) {
            int h = sign * hours;
            offset = (double)h * 3600.0
                   + (h >= 0 ? minutes * 60.0 : -minutes * 60.0);
        } else {
            offset = (double)(sign * hours) * 3600.0;
        }
        break;
    }
    /* One localtime call first: it makes the POSIX `timezone` global reflect
     * the TZ in effect (Python's time.timezone does the same via tzset()). */
    probe = 0;
    localtime(&probe);
    base = mktime(&stamp);
    if (base == (time_t)-1) return 0;   /* CPython raises OverflowError */
    *out = (double)base - (double)RM_STD_OFFSET() - offset;
    return 1;
}

/* ---- time helpers (local day keys / midnight / iso) ---- */

static time_t rm_to_time_t(double ts)
{
    return (time_t)floor(ts);           /* CPython converts with ROUND_FLOOR */
}

static double rm_local_midnight(double ts, int days_back)
{
    time_t t = rm_to_time_t(ts);
    struct tm lt, copy;
    if (localtime_r(&t, &lt) == NULL) {
        t = 0;
        if (localtime_r(&t, &lt) == NULL) return 0.0;
    }
    copy = lt;
    copy.tm_mday -= days_back;          /* mktime normalises out-of-range days */
    copy.tm_hour = 0;
    copy.tm_min = 0;
    copy.tm_sec = 0;
    copy.tm_isdst = -1;
    return (double)mktime(&copy);
}

static void rm_day_key(double ts, char *out, size_t cap)
{
    time_t t = rm_to_time_t(ts);
    struct tm lt;
    if (localtime_r(&t, &lt) == NULL) {
        t = 0;
        if (localtime_r(&t, &lt) == NULL) {
            out[0] = '\0';
            return;
        }
    }
    if (strftime(out, cap, "%Y-%m-%d", &lt) == 0) out[0] = '\0';
}

/* time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)); negative epochs
 * format too (same libc localtime on both sides). */
static void rm_iso(double ts, char *out, size_t cap)
{
    time_t t = rm_to_time_t(ts);
    struct tm lt;
    if (localtime_r(&t, &lt) == NULL) {
        out[0] = '\0';
        return;
    }
    if (strftime(out, cap, "%Y-%m-%d %H:%M:%S", &lt) == 0) out[0] = '\0';
}

/* ---- lifecycle ---- */

Remaining *remaining_new(void)
{
    Remaining *st = (Remaining *)rm_calloc(1, sizeof *st);
    map_init(&st->usage);
    return st;
}

void remaining_free(Remaining *st)
{
    size_t i;
    if (!st) return;
    for (i = 0; i < st->usage.cap; i++) {
        RmPair *p = st->usage.ents[i].key
            ? (RmPair *)st->usage.ents[i].val : NULL;
        if (!p) continue;
        free(p->uid);
        free(p->model);
        free(p->ats);
        free(p->cum);
        free(p);
    }
    map_free(&st->usage);
    for (i = 0; i < st->n_events; i++) {
        free(st->events[i].account);
        free(st->events[i].model);
        free(st->events[i].realm);
    }
    free(st->events);
    free(st);
}

/* ---- fold (_fold_remaining over the row's parsed tree) ---- */

void remaining_fold_json(Remaining *st, const JVal *obj)
{
    const JVal *accv, *modv, *atv, *statusv, *msgv, *ov;
    const char *uid, *model;
    double at, reset, window_start;
    long long tokens, sample;
    RmPair *buf;
    RmAnchorList anchors;
    Buf text;

    if (!st || !obj || !jis_obj(obj)) return;
    accv = jget(obj, "account");
    modv = jget(obj, "model");
    uid = (accv && jis_str(accv)) ? jstr(accv) : NULL;
    model = (modv && jis_str(modv)) ? jstr(modv) : NULL;
    if (!uid || !uid[0] || !model || !model[0]) return;
    atv = jget(obj, "at");
    if (!rm_truthy(atv)) return;        /* `at or 0` falsy -> row skipped */
    if (!rm_to_float(atv, &at)) return;
    statusv = jget(obj, "status");
    if (jis_num(statusv) && jnum(statusv) == 429.0) {
        msgv = jget(obj, "message");
        buf_init(&text);
        rm_to_text(&text, msgv);        /* str(row.get("message") or "") */
        if (!rm_parse_rate_limit_reset(text.data ? text.data : "", &reset)) {
            buf_free(&text);
            return;
        }
        buf_free(&text);
        /* Sample truncated at the segment start: chain caps anchor the window
         * at the previous reset, otherwise reset - 24h. */
        rm_anchor_list_init(&anchors);
        rm_collect_anchors(st, uid, model, &anchors);
        window_start = rm_cap_window_start(&anchors, reset);
        rm_anchor_list_free(&anchors);
        buf = rm_pair_find(st, uid, model);
        sample = rm_range_sum(buf, window_start, at);
        buf_init(&text);
        rm_to_text(&text, jget(obj, "realm"));
        rm_merge_event(st, at, reset, uid, model,
                       text.data ? text.data : "", sample, RM_SRC_LOG);
        buf_free(&text);
        return;
    }
    if (rm_truthy(jget(obj, "error")) || rm_truthy(jget(obj, "usage_missing")))
        return;
    ov = jget(obj, "outcome");
    if (ov && jis_str(ov) && jstr(ov)[0] && strcmp(jstr(ov), "client_aborted") == 0)
        return;
    if (!rm_as_ll(jget(obj, "total_tokens"), &tokens)) tokens = 0;
    buf = rm_pair_get_or_new(st, uid, model);
    rm_pair_append(buf, at, tokens);
    rm_pair_trim(buf, at - RM_KEEP_SECONDS);
}

/* _merge_event(state, event, "file") for one limit-events file line. */
void remaining_merge_event_json(Remaining *st, const JVal *ev)
{
    Buf ub, mb, rb;
    double at = 0.0, reset = 0.0;
    long long sample = 0;

    if (!st || !ev || !jis_obj(ev)) return;
    buf_init(&ub);
    buf_init(&mb);
    buf_init(&rb);
    rm_to_text(&ub, jget(ev, "account"));
    rm_to_text(&mb, jget(ev, "model"));
    if (ub.len == 0 || mb.len == 0) goto done;
    if (!rm_to_float(jget(ev, "reset"), &reset)) goto done;
    if (!rm_to_float(jget(ev, "at"), &at)) goto done;
    if (!rm_as_ll(jget(ev, "sample"), &sample)) sample = 0;
    rm_to_text(&rb, jget(ev, "realm"));
    rm_merge_event(st, at, reset, ub.data, mb.data, rb.data, sample,
                   RM_SRC_FILE);
done:
    buf_free(&ub);
    buf_free(&mb);
    buf_free(&rb);
}

/* ---- budgets (_budget_means / _remaining_budgets) ---- */

typedef struct {
    char *account;                      /* borrowed from the event table */
    long long *samples;
    size_t n, cap;
} RmAcctSamples;

typedef struct {
    char *realm;
    char *model;
    RmAcctSamples *accts;               /* today's samples, per account */
    size_t n_accts, cap_accts;
    int has_daily[2];                   /* keys[1] / keys[2] entries */
    double d_avg[2], d_min[2], d_max[2];
    long long d_n[2];
    int n_rows;
    struct {
        const char *day;
        int w;
        long long avg;
        long long n;
    } rows[3];
    int has_out;
    long long out_avg, out_min, out_max, out_n;
} RmPairBudget;

static void rm_pair_budget_free(RmPairBudget *pb)
{
    size_t i;
    if (!pb) return;
    for (i = 0; i < pb->n_accts; i++) free(pb->accts[i].samples);
    free(pb->accts);
    free(pb->realm);
    free(pb->model);
    free(pb);
}

static RmPairBudget *rm_pair_budget_get(Map *m, const char *realm,
                                        const char *model)
{
    Buf key;
    RmPairBudget *pb;
    buf_init(&key);
    rm_key_build(&key, realm, model);
    pb = (RmPairBudget *)map_get(m, key.data);
    if (!pb) {
        pb = (RmPairBudget *)rm_calloc(1, sizeof *pb);
        pb->realm = rm_strdup(realm);
        pb->model = rm_strdup(model);
        map_put(m, key.data, pb);
    }
    buf_free(&key);
    return pb;
}

static void rm_acct_samples_add(RmPairBudget *pb, const char *account,
                                long long sample)
{
    size_t i;
    for (i = 0; i < pb->n_accts; i++) {
        if (strcmp(pb->accts[i].account, account) == 0) {
            RmAcctSamples *as = &pb->accts[i];
            if (as->n == as->cap) {
                as->cap = as->cap ? as->cap * 2 : 4;
                as->samples = (long long *)rm_realloc(
                    as->samples, as->cap * sizeof(long long));
            }
            as->samples[as->n++] = sample;
            return;
        }
    }
    if (pb->n_accts == pb->cap_accts) {
        pb->cap_accts = pb->cap_accts ? pb->cap_accts * 2 : 4;
        pb->accts = (RmAcctSamples *)rm_realloc(
            pb->accts, pb->cap_accts * sizeof(RmAcctSamples));
    }
    {
        RmAcctSamples *as = &pb->accts[pb->n_accts++];
        as->account = (char *)account;
        as->cap = 4;
        as->n = 1;
        as->samples = (long long *)rm_alloc(as->cap * sizeof(long long));
        as->samples[0] = sample;
    }
}

/* {账号: [样本]} -> {avg, min, max, n}: per-account means first (banker's
 * round), then the cross-account mean; min/max/n are of the account means. */
static void rm_budget_means(const RmPairBudget *pb, long long *avg,
                            long long *mn, long long *mx, long long *n_out)
{
    long long *means = (long long *)rm_alloc(pb->n_accts * sizeof(long long));
    long long total = 0;
    size_t i, k;
    for (i = 0; i < pb->n_accts; i++) {
        const RmAcctSamples *as = &pb->accts[i];
        long long sum = 0;
        for (k = 0; k < as->n; k++) sum += as->samples[k];
        means[i] = (long long)py_round_half_even((double)sum / (double)as->n);
    }
    *mn = means[0];
    *mx = means[0];
    for (i = 0; i < pb->n_accts; i++) {
        if (means[i] < *mn) *mn = means[i];
        if (means[i] > *mx) *mx = means[i];
        total += means[i];
    }
    *avg = (long long)py_round_half_even((double)total / (double)pb->n_accts);
    *n_out = (long long)pb->n_accts;
    free(means);
}

/* _estimate_budget_entry_ok: {avg,min,max,n}, n a strict JSON int > 0. */
static int rm_daily_entry_ok(const JVal *entry, double *avg, double *mn,
                             double *mx, long long *n)
{
    const JVal *nv = jget(entry, "n");
    const JVal *av = jget(entry, "avg");
    const JVal *mnv = jget(entry, "min");
    const JVal *mxv = jget(entry, "max");
    if (!jis_obj(entry)) return 0;
    if (!jnum_is_int(nv) || jint(nv) <= 0) return 0;
    if (!jis_num(av) || !jis_num(mnv) || !jis_num(mxv)) return 0;
    *avg = jnum(av);
    *mn = jnum(mnv);
    *mx = jnum(mxv);
    *n = jint(nv);
    return 1;
}

/* Weighted near-days estimate: [today, yesterday, day before] with weights
 * (6, 3, 1), normalised over the days that have samples. */
static void rm_budget_finalize(RmPairBudget *pb, const char *const *keys)
{
    static const int weights[3] = { 6, 3, 1 };
    double raw_mn[3], raw_mx[3];
    long long weight = 0, num = 0, nsum = 0;
    long long avg0 = 0, mn0 = 0, mx0 = 0, n0 = 0;
    int idx, k;

    pb->n_rows = 0;
    if (pb->n_accts > 0) rm_budget_means(pb, &avg0, &mn0, &mx0, &n0);
    for (idx = 0; idx < 3; idx++) {
        double a = 0.0, mn = 0.0, mx = 0.0;
        long long n = 0;
        if (idx == 0) {
            if (pb->n_accts == 0) continue;
            a = (double)avg0;
            mn = (double)mn0;
            mx = (double)mx0;
            n = n0;
        } else {
            int di = idx - 1;
            if (!pb->has_daily[di]) continue;
            a = pb->d_avg[di];
            mn = pb->d_min[di];
            mx = pb->d_max[di];
            n = pb->d_n[di];
        }
        if (n <= 0) continue;
        k = pb->n_rows;
        pb->rows[k].day = keys[idx];
        pb->rows[k].w = weights[idx];
        pb->rows[k].avg = (long long)py_round_half_even(a);
        pb->rows[k].n = n;
        raw_mn[k] = mn;
        raw_mx[k] = mx;
        pb->n_rows++;
    }
    if (pb->n_rows == 0) {
        pb->has_out = 0;
        return;
    }
    for (k = 0; k < pb->n_rows; k++) {
        weight += pb->rows[k].w;
        num += (long long)pb->rows[k].w * pb->rows[k].avg;
        nsum += pb->rows[k].n;
    }
    pb->out_avg = (long long)py_round_half_even((double)num / (double)weight);
    pb->out_min = (long long)py_round_half_even(raw_mn[0]);
    pb->out_max = (long long)py_round_half_even(raw_mx[0]);
    for (k = 1; k < pb->n_rows; k++) {
        long long m1 = (long long)py_round_half_even(raw_mn[k]);
        long long m2 = (long long)py_round_half_even(raw_mx[k]);
        if (m1 < pb->out_min) pb->out_min = m1;
        if (m2 > pb->out_max) pb->out_max = m2;
    }
    pb->out_n = nsum;
    pb->has_out = 1;
}

static void rm_build_budgets(const Remaining *st, const Map *realm_of,
                             const JVal *daily,
                             const char *const *keys, Map *out)
{
    Map tmp;
    size_t i, j;
    int idx;

    map_init(&tmp);
    /* Today: samples from the in-memory events (sample > 0, local day). */
    for (i = 0; i < st->n_events; i++) {
        const RmEvent *e = &st->events[i];
        char day[32];
        const char *realm;
        RmPairBudget *pb;
        if (e->sample <= 0) continue;
        rm_day_key(e->at, day, sizeof day);
        if (strcmp(day, keys[0]) != 0) continue;
        realm = e->realm[0] ? e->realm
                            : (const char *)map_get(realm_of, e->account);
        if (!realm) realm = "";
        pb = rm_pair_budget_get(&tmp, realm, e->model);
        rm_acct_samples_add(pb, e->account, e->sample);
    }
    /* Yesterday / the day before: the archived day budgets, keyed
     * "realm\x1fmodel", read straight out of estimate-daily.json. */
    for (idx = 1; idx < 3; idx++) {
        const JVal *days, *record, *budgets_obj;
        if (!daily || !jis_obj(daily)) break;
        days = jget(daily, "days");
        if (!jis_obj(days)) break;
        record = jget(days, keys[idx]);
        if (!jis_obj(record)) continue;
        budgets_obj = jget(record, "budgets");
        if (!jis_obj(budgets_obj)) continue;
        for (j = 0; j < jobj_len(budgets_obj); j++) {
            const char *pair_key = jobj_key(budgets_obj, j);
            const JVal *entry = jobj_val(budgets_obj, j);
            const char *sep, *model;
            double avg, mn, mx;
            long long n;
            size_t rlen;
            RmPairBudget *pb;
            Buf rb;
            if (!pair_key) continue;
            if (!rm_daily_entry_ok(entry, &avg, &mn, &mx, &n)) continue;
            sep = strchr(pair_key, '\x1f');
            if (sep) {
                rlen = (size_t)(sep - pair_key);
                model = sep + 1;
            } else {
                rlen = strlen(pair_key);
                model = "";
            }
            if (!model[0]) continue;
            buf_init(&rb);
            buf_put(&rb, pair_key, rlen);
            pb = rm_pair_budget_get(&tmp, rb.data, model);
            buf_free(&rb);
            pb->has_daily[idx - 1] = 1;
            pb->d_avg[idx - 1] = avg;
            pb->d_min[idx - 1] = mn;
            pb->d_max[idx - 1] = mx;
            pb->d_n[idx - 1] = n;
        }
    }
    for (i = 0; i < tmp.cap; i++) {
        RmPairBudget *pb = tmp.ents[i].key
            ? (RmPairBudget *)tmp.ents[i].val : NULL;
        if (!pb) continue;
        rm_budget_finalize(pb, keys);
        if (pb->has_out) {
            Buf key;
            buf_init(&key);
            rm_key_build(&key, pb->realm, pb->model);
            map_put(out, key.data, pb);
            buf_free(&key);
        } else {
            rm_pair_budget_free(pb);
        }
    }
    map_free(&tmp);                     /* frees the temporary keys only */
}

/* ---- payload (_remaining_payload_locked) ---- */

typedef struct {
    char *uid;
    char *model;
    RmAnchorList list;
} RmAnchorGroup;

typedef struct {
    double start, end;
    long long used;
    int capped, current;
} RmSegment;

typedef struct {
    const char *uid;
    const char *nickname;
    const char *realm;
    const char *model;
    long long used;
    int has_budget;
    long long budget, budget_min, budget_max, samples;
    int has_remaining;
    long long remaining;
    int has_pct;
    double pct;
    double window_start;
    int has_reset;
    double reset_at;
    int cooling, estimated;
    RmSegment segs[2];
    int nsegs;
    size_t order;
} RmRow;

typedef struct {
    const char *realm;
    const char *model;
    int has_total;
    long long total_remaining;
    int has_budget;
    long long budget;
    long long pairs, available, cooling, unknown, estimated;
} RmTotal;

static RmAnchorGroup *rm_anchor_group_get(Map *m, const char *uid,
                                          const char *model)
{
    Buf key;
    RmAnchorGroup *g;
    buf_init(&key);
    rm_key_build(&key, uid, model);
    g = (RmAnchorGroup *)map_get(m, key.data);
    if (!g) {
        g = (RmAnchorGroup *)rm_calloc(1, sizeof *g);
        g->uid = rm_strdup(uid);
        g->model = rm_strdup(model);
        rm_anchor_list_init(&g->list);
        map_put(m, key.data, g);
    }
    buf_free(&key);
    return g;
}

static const RmAnchorGroup *rm_anchor_group_find(const Map *m, const char *uid,
                                                 const char *model)
{
    Buf key;
    const RmAnchorGroup *g;
    buf_init(&key);
    rm_key_build(&key, uid, model);
    g = (const RmAnchorGroup *)map_get(m, key.data);
    buf_free(&key);
    return g;
}

static int rm_active_has(const Map *active, const char *realm,
                         const char *model)
{
    Buf key;
    int found;
    buf_init(&key);
    rm_key_build(&key, realm, model);
    found = map_get(active, key.data) != NULL;
    buf_free(&key);
    return found;
}

static void rm_active_add(Map *active, const char *realm, const char *model)
{
    Buf key;
    buf_init(&key);
    rm_key_build(&key, realm, model);
    map_put(active, key.data, (void *)1);
    buf_free(&key);
}

static RmPairBudget *rm_budget_find(const Map *m, const char *realm,
                                    const char *model)
{
    Buf key;
    RmPairBudget *pb;
    buf_init(&key);
    rm_key_build(&key, realm, model);
    pb = (RmPairBudget *)map_get(m, key.data);
    buf_free(&key);
    return pb;
}

static RmTotal *rm_total_get(Map *totals, const char *realm,
                             const char *model)
{
    Buf key;
    RmTotal *t;
    buf_init(&key);
    rm_key_build(&key, realm, model);
    t = (RmTotal *)map_get(totals, key.data);
    if (!t) {
        t = (RmTotal *)rm_calloc(1, sizeof *t);
        t->realm = realm;
        t->model = model;
        map_put(totals, key.data, t);
    }
    buf_free(&key);
    return t;
}

static int rm_cmp_account(const void *pa, const void *pb)
{
    const struct rm_account *a = *(struct rm_account *const *)pa;
    const struct rm_account *b = *(struct rm_account *const *)pb;
    return strcmp(a->uid, b->uid);
}

static int rm_cmp_str(const void *a, const void *b)
{
    return strcmp(*(const char *const *)a, *(const char *const *)b);
}

static void rm_models_push(const char ***arr, size_t *n, size_t *cap,
                           const char *model)
{
    if (*n == *cap) {
        *cap = *cap ? *cap * 2 : 8;
        *arr = (const char **)rm_realloc((void *)*arr,
                                         *cap * sizeof(const char *));
    }
    (*arr)[(*n)++] = model;
}

/* rows.sort(key=(remaining is None, remaining or 0, realm, nickname, model));
 * the original index breaks full ties, mirroring Python's stable sort. */
static int rm_cmp_row(const void *pa, const void *pb)
{
    const RmRow *x = (const RmRow *)pa;
    const RmRow *y = (const RmRow *)pb;
    int k1x = x->has_remaining ? 0 : 1;
    int k1y = y->has_remaining ? 0 : 1;
    long long k2x, k2y;
    int c;
    if (k1x != k1y) return k1x - k1y;
    k2x = x->has_remaining ? x->remaining : 0;
    k2y = y->has_remaining ? y->remaining : 0;
    if (k2x != k2y) return k2x < k2y ? -1 : 1;
    c = strcmp(x->realm, y->realm);
    if (c) return c;
    c = strcmp(x->nickname, y->nickname);
    if (c) return c;
    c = strcmp(x->model, y->model);
    if (c) return c;
    if (x->order != y->order) return x->order < y->order ? -1 : 1;
    return 0;
}

static int rm_cmp_budget_out(const void *pa, const void *pb)
{
    const RmPairBudget *a = *(RmPairBudget *const *)pa;
    const RmPairBudget *b = *(RmPairBudget *const *)pb;
    int c = strcmp(a->realm, b->realm);
    if (c) return c;
    return strcmp(a->model, b->model);
}

/* models.sort(key=(total_remaining is None, -(total_remaining or 0),
 * realm, model)). */
static int rm_cmp_total_out(const void *pa, const void *pb)
{
    const RmTotal *a = *(RmTotal *const *)pa;
    const RmTotal *b = *(RmTotal *const *)pb;
    int k1a = a->has_total ? 0 : 1;
    int k1b = b->has_total ? 0 : 1;
    long long k2a, k2b;
    int c;
    if (k1a != k1b) return k1a - k1b;
    k2a = a->has_total ? -a->total_remaining : 0;
    k2b = b->has_total ? -b->total_remaining : 0;
    if (k2a != k2b) return k2a < k2b ? -1 : 1;
    c = strcmp(a->realm, b->realm);
    if (c) return c;
    return strcmp(a->model, b->model);
}

void remaining_payload(const Remaining *st, double now, const Map *accounts,
                       const JVal *daily, JW *out)
{
    struct rm_account **accts;
    size_t n_accts = 0;
    size_t i, j, k;
    Map realm_of, budgets, anchors, active, totals;
    RmRow *rows = NULL;
    size_t n_rows = 0, cap_rows = 0;
    char keys[3][32];
    const char *keyptrs[3];
    double floor_ts;
    char iso[64];
    RmPairBudget **blist = NULL;
    size_t n_blist = 0;
    RmTotal **tlist = NULL;
    size_t n_tlist = 0;

    if (!st || !out) return;

    /* Enabled accounts with a uid; iterate deterministically (uid ascending)
     * because the Map keeps no insertion order (rows ties would otherwise be
     * machine-dependent). */
    accts = (struct rm_account **)rm_alloc(
        (accounts && accounts->len ? accounts->len : 1) * sizeof(*accts));
    if (accounts) {
        for (i = 0; i < accounts->cap; i++) {
            struct rm_account *a = accounts->ents[i].key
                ? (struct rm_account *)accounts->ents[i].val : NULL;
            if (!a || !a->enabled || !a->uid || !a->uid[0]) continue;
            accts[n_accts++] = a;
        }
    }
    qsort(accts, n_accts, sizeof(*accts), rm_cmp_account);

    map_init(&realm_of);
    for (i = 0; i < n_accts; i++)
        map_put(&realm_of, accts[i]->uid,
                (void *)(accts[i]->realm ? accts[i]->realm : ""));

    for (i = 0; i < 3; i++) {
        rm_day_key(rm_local_midnight(now, (int)i), keys[i], sizeof keys[i]);
        keyptrs[i] = keys[i];
    }

    map_init(&budgets);
    rm_build_budgets(st, &realm_of, daily, keyptrs, &budgets);

    /* Anchors: the event table plus the accounts' persisted model_caps. */
    map_init(&anchors);
    for (i = 0; i < st->n_events; i++) {
        const RmEvent *e = &st->events[i];
        RmAnchorGroup *g = rm_anchor_group_get(&anchors, e->account, e->model);
        rm_anchor_add(&g->list, e->at, e->reset);
    }
    for (i = 0; i < n_accts; i++) {
        struct rm_account *a = accts[i];
        const JVal *caps = a->model_caps;
        if (!jis_obj(caps)) continue;
        for (j = 0; j < jobj_len(caps); j++) {
            const char *model = jobj_key(caps, j);
            const JVal *entry = jobj_val(caps, j);
            double at, reset;
            RmAnchorGroup *g;
            if (!model || !jis_obj(entry)) continue;
            if (!rm_to_float(jget(entry, "at"), &at)) continue;
            if (!rm_to_float(jget(entry, "reset"), &reset)) continue;
            g = rm_anchor_group_get(&anchors, a->uid, model);
            rm_anchor_add(&g->list, at, reset);
        }
    }

    /* Activity floor: local midnight of yesterday. */
    floor_ts = rm_local_midnight(now, 0) - RM_WINDOW_SECONDS;

    map_init(&active);
    for (i = 0; i < st->usage.cap; i++) {
        const RmPair *p = st->usage.ents[i].key
            ? (const RmPair *)st->usage.ents[i].val : NULL;
        const char *realm;
        if (!p) continue;
        realm = (const char *)map_get(&realm_of, p->uid);
        if (!realm) continue;           /* not an enabled account */
        if (p->len == 0) continue;
        if (p->ats[p->len - 1] >= floor_ts) rm_active_add(&active, realm,
                                                          p->model);
    }
    for (i = 0; i < anchors.cap; i++) {
        const RmAnchorGroup *g = anchors.ents[i].key
            ? (const RmAnchorGroup *)anchors.ents[i].val : NULL;
        const char *realm;
        if (!g) continue;
        realm = (const char *)map_get(&realm_of, g->uid);
        if (!realm) continue;
        for (j = 0; j < g->list.n; j++) {
            double m = g->list.v[j].reset > g->list.v[j].at
                ? g->list.v[j].reset : g->list.v[j].at;
            if (m >= floor_ts) {
                rm_active_add(&active, realm, g->model);
                break;
            }
        }
    }

    map_init(&totals);
    for (i = 0; i < n_accts; i++) {
        struct rm_account *a = accts[i];
        const char *uid = a->uid;
        const char *realm = (const char *)map_get(&realm_of, uid);
        const char *nickname = a->nickname ? a->nickname : "";
        const char **models = NULL;
        size_t n_models = 0, cap_models = 0;
        if (!realm) realm = "";

        /* Every budgeted model of the account's realm that is active, plus
         * every active model the account itself used. */
        for (j = 0; j < budgets.cap; j++) {
            const RmPairBudget *pb = budgets.ents[j].key
                ? (const RmPairBudget *)budgets.ents[j].val : NULL;
            if (!pb) continue;
            if (strcmp(pb->realm, realm) != 0) continue;
            if (!rm_active_has(&active, pb->realm, pb->model)) continue;
            rm_models_push(&models, &n_models, &cap_models, pb->model);
        }
        for (j = 0; j < st->usage.cap; j++) {
            const RmPair *p = st->usage.ents[j].key
                ? (const RmPair *)st->usage.ents[j].val : NULL;
            if (!p) continue;
            if (strcmp(p->uid, uid) != 0) continue;
            if (!rm_active_has(&active, realm, p->model)) continue;
            rm_models_push(&models, &n_models, &cap_models, p->model);
        }
        if (n_models > 1) {
            qsort(models, n_models, sizeof(*models), rm_cmp_str);
            k = 0;
            for (j = 0; j < n_models; j++) {
                if (k == 0 || strcmp(models[j], models[k - 1]) != 0)
                    models[k++] = models[j];
            }
            n_models = k;
        }

        for (j = 0; j < n_models; j++) {
            const char *model = models[j];
            const RmAnchorGroup *g = rm_anchor_group_find(&anchors, uid, model);
            const RmAnchorList *al = g ? &g->list : NULL;
            const RmPair *buf = rm_pair_find(st, uid, model);
            RmPairBudget *pb;
            int has_last = 0, has_up = 0;
            double last_reset = 0.0, last_at = 0.0;
            double up_reset = 0.0, up_at = 0.0;
            double window_start = 0.0, reset_at = 0.0;
            long long used = 0;
            int cooling = 0, estimated = 0, has_reset = 0;
            RmSegment segs[2];
            int nsegs = 0;
            int has_remaining = 0, has_pct = 0;
            long long remaining = 0;
            double pct = 0.0;
            RmRow *row;

            if (al) {
                for (k = 0; k < al->n; k++) {
                    double r = al->v[k].reset;
                    if (r <= now) {
                        if (!has_last || r > last_reset) {
                            has_last = 1;
                            last_reset = r;
                            last_at = al->v[k].at;
                        }
                    } else if (r > now) {
                        if (!has_up || r < up_reset) {
                            has_up = 1;
                            up_reset = r;
                            up_at = al->v[k].at;
                        }
                    }
                }
            }

            if (has_up) {
                /* Cooling: the window is spent, the quota returns at the
                 * reset; the accounting stops at the cap moment. */
                double end;
                reset_at = up_reset;
                window_start = rm_cap_window_start(al, reset_at);
                end = up_at != 0.0 ? up_at : reset_at;
                if (!(end < now)) end = now;    /* min(now, end) */
                used = rm_range_sum(buf, window_start, end);
                segs[0].start = window_start;
                segs[0].end = end;
                segs[0].used = used;
                segs[0].capped = 1;
                segs[0].current = 1;
                nsegs = 1;
                cooling = 1;
                estimated = 0;
                has_reset = 1;
            } else if (has_last) {
                double alt = now - RM_WINDOW_SECONDS;
                reset_at = last_reset;
                window_start = reset_at > alt ? reset_at : alt;
                used = rm_range_sum(buf, window_start, now);
                cooling = 0;
                estimated = reset_at < alt;
                has_reset = 1;
                if (!estimated) {
                    /* Segments add up: the previous window (cut at its cap)
                     * plus the current one (from the reset on). */
                    if (last_at > 0) {
                        double prev_end = reset_at < last_at
                            ? reset_at : last_at;   /* min(last.at, reset_at) */
                        double prev_start = rm_cap_window_start(al, reset_at);
                        segs[nsegs].start = prev_start;
                        segs[nsegs].end = prev_end;
                        segs[nsegs].used = rm_range_sum(buf, prev_start,
                                                        prev_end);
                        segs[nsegs].capped = 1;
                        segs[nsegs].current = 0;
                        nsegs++;
                    }
                    segs[nsegs].start = reset_at;
                    segs[nsegs].end = now;
                    segs[nsegs].used = used;
                    segs[nsegs].capped = 0;
                    segs[nsegs].current = 1;
                    nsegs++;
                }
            } else {
                window_start = now - RM_WINDOW_SECONDS;
                used = rm_range_sum(buf, window_start, now);
                reset_at = 0.0;
                cooling = 0;
                estimated = 1;
                has_reset = 0;
            }

            pb = rm_budget_find(&budgets, realm, model);
            if (cooling) {
                has_remaining = 1;
                remaining = 0;          /* no budget: still 0 while cooling */
                if (pb && pb->out_avg != 0) {
                    has_pct = 1;
                    pct = py_round1((double)used * 100.0
                                    / (double)pb->out_avg);
                }
            } else if (pb) {
                long long diff = pb->out_avg - used;
                has_remaining = 1;
                remaining = diff < 0 ? 0 : diff;
                if (pb->out_avg != 0) {
                    has_pct = 1;
                    pct = py_round1((double)used * 100.0
                                    / (double)pb->out_avg);
                }
            }

            if (n_rows == cap_rows) {
                cap_rows = cap_rows ? cap_rows * 2 : 32;
                rows = (RmRow *)rm_realloc(rows, cap_rows * sizeof(RmRow));
            }
            row = &rows[n_rows];
            memset(row, 0, sizeof *row);
            row->uid = uid;
            row->nickname = nickname;
            row->realm = realm;
            row->model = model;
            row->used = used;
            if (pb) {
                row->has_budget = 1;
                row->budget = pb->out_avg;
                row->budget_min = pb->out_min;
                row->budget_max = pb->out_max;
                row->samples = pb->out_n;
            }
            row->has_remaining = has_remaining;
            row->remaining = remaining;
            row->has_pct = has_pct;
            row->pct = pct;
            row->window_start = window_start;
            row->has_reset = has_reset;
            row->reset_at = reset_at;
            row->cooling = cooling;
            row->estimated = estimated;
            row->nsegs = nsegs;
            for (k = 0; k < (size_t)nsegs; k++) row->segs[k] = segs[k];
            row->order = n_rows;
            n_rows++;

            {
                RmTotal *t = rm_total_get(&totals, realm, model);
                t->has_budget = pb != NULL;
                if (pb) t->budget = pb->out_avg;
                t->pairs++;
                if (cooling) t->cooling++;
                if (estimated) t->estimated++;
                if (!has_remaining) {
                    t->unknown++;
                } else {
                    t->has_total = 1;
                    t->total_remaining += remaining;
                    if (!cooling) t->available++;
                }
            }
        }
        free(models);
    }

    if (n_rows > 1) qsort(rows, n_rows, sizeof(RmRow), rm_cmp_row);

    blist = (RmPairBudget **)rm_alloc((budgets.len ? budgets.len : 1)
                                      * sizeof(*blist));
    for (j = 0; j < budgets.cap; j++) {
        RmPairBudget *pb = budgets.ents[j].key
            ? (RmPairBudget *)budgets.ents[j].val : NULL;
        if (pb) blist[n_blist++] = pb;
    }
    if (n_blist > 1) qsort(blist, n_blist, sizeof(*blist), rm_cmp_budget_out);

    tlist = (RmTotal **)rm_alloc((totals.len ? totals.len : 1) * sizeof(*tlist));
    for (j = 0; j < totals.cap; j++) {
        RmTotal *t = totals.ents[j].key
            ? (RmTotal *)totals.ents[j].val : NULL;
        if (t) tlist[n_tlist++] = t;
    }
    if (n_tlist > 1) qsort(tlist, n_tlist, sizeof(*tlist), rm_cmp_total_out);

    /* ---- output, key order per _remaining_payload_locked ---- */
    jw_obj_begin(out);
    jw_key(out, "window_seconds");
    jw_int(out, (long long)RM_WINDOW_SECONDS);
    jw_key(out, "generated_at");
    jw_double(out, now);
    jw_key(out, "generated_iso");
    rm_iso(now, iso, sizeof iso);
    jw_str(out, iso);
    jw_key(out, "activity_floor");
    jw_double(out, floor_ts);
    jw_key(out, "activity_floor_iso");
    rm_iso(floor_ts, iso, sizeof iso);
    jw_str(out, iso);

    jw_key(out, "budgets");
    jw_arr_begin(out);
    for (i = 0; i < n_blist; i++) {
        RmPairBudget *pb = blist[i];
        jw_obj_begin(out);
        jw_key(out, "avg");
        jw_int(out, pb->out_avg);
        jw_key(out, "min");
        jw_int(out, pb->out_min);
        jw_key(out, "max");
        jw_int(out, pb->out_max);
        jw_key(out, "n");
        jw_int(out, pb->out_n);
        jw_key(out, "days");
        jw_arr_begin(out);
        for (k = 0; k < (size_t)pb->n_rows; k++) {
            jw_obj_begin(out);
            jw_key(out, "day");
            jw_str(out, pb->rows[k].day);
            jw_key(out, "w");
            jw_int(out, pb->rows[k].w);
            jw_key(out, "avg");
            jw_int(out, pb->rows[k].avg);
            jw_key(out, "n");
            jw_int(out, pb->rows[k].n);
            jw_obj_end(out);
        }
        jw_arr_end(out);
        jw_key(out, "realm");
        jw_str(out, pb->realm);
        jw_key(out, "model");
        jw_str(out, pb->model);
        jw_key(out, "active");
        jw_bool(out, rm_active_has(&active, pb->realm, pb->model));
        jw_obj_end(out);
    }
    jw_arr_end(out);

    jw_key(out, "models");
    jw_arr_begin(out);
    for (i = 0; i < n_tlist; i++) {
        RmTotal *t = tlist[i];
        jw_obj_begin(out);
        jw_key(out, "realm");
        jw_str(out, t->realm);
        jw_key(out, "model");
        jw_str(out, t->model);
        jw_key(out, "total_remaining");
        if (t->has_total) jw_int(out, t->total_remaining);
        else jw_null(out);
        jw_key(out, "budget");
        if (t->has_budget) jw_int(out, t->budget);
        else jw_null(out);
        jw_key(out, "pairs");
        jw_int(out, t->pairs);
        jw_key(out, "available");
        jw_int(out, t->available);
        jw_key(out, "cooling");
        jw_int(out, t->cooling);
        jw_key(out, "unknown");
        jw_int(out, t->unknown);
        jw_key(out, "estimated");
        jw_int(out, t->estimated);
        jw_obj_end(out);
    }
    jw_arr_end(out);

    jw_key(out, "rows");
    jw_arr_begin(out);
    for (i = 0; i < n_rows; i++) {
        const RmRow *r = &rows[i];
        jw_obj_begin(out);
        jw_key(out, "uid");
        jw_str(out, r->uid);
        jw_key(out, "nickname");
        jw_str(out, r->nickname);
        jw_key(out, "realm");
        jw_str(out, r->realm);
        jw_key(out, "model");
        jw_str(out, r->model);
        jw_key(out, "used");
        jw_int(out, r->used);
        jw_key(out, "budget");
        if (r->has_budget) jw_int(out, r->budget);
        else jw_null(out);
        jw_key(out, "budget_min");
        if (r->has_budget) jw_int(out, r->budget_min);
        else jw_null(out);
        jw_key(out, "budget_max");
        if (r->has_budget) jw_int(out, r->budget_max);
        else jw_null(out);
        jw_key(out, "samples");
        jw_int(out, r->has_budget ? r->samples : 0);
        jw_key(out, "remaining");
        if (r->has_remaining) jw_int(out, r->remaining);
        else jw_null(out);
        jw_key(out, "used_pct");
        if (r->has_pct) jw_double(out, r->pct);
        else jw_null(out);
        jw_key(out, "window_start");
        jw_double(out, r->window_start);
        jw_key(out, "window_iso");
        rm_iso(r->window_start, iso, sizeof iso);
        jw_str(out, iso);
        jw_key(out, "reset_at");
        if (r->has_reset) jw_double(out, r->reset_at);
        else jw_null(out);
        jw_key(out, "reset_iso");
        if (r->has_reset) {
            rm_iso(r->reset_at, iso, sizeof iso);
            jw_str(out, iso);
        } else {
            jw_null(out);
        }
        jw_key(out, "cooling");
        jw_bool(out, r->cooling);
        jw_key(out, "estimated");
        jw_bool(out, r->estimated);
        jw_key(out, "segments");
        jw_arr_begin(out);
        for (k = 0; k < (size_t)r->nsegs; k++) {
            jw_obj_begin(out);
            jw_key(out, "start");
            jw_double(out, r->segs[k].start);
            jw_key(out, "end");
            jw_double(out, r->segs[k].end);
            jw_key(out, "used");
            jw_int(out, r->segs[k].used);
            jw_key(out, "capped");
            jw_bool(out, r->segs[k].capped);
            jw_key(out, "current");
            jw_bool(out, r->segs[k].current);
            jw_key(out, "start_iso");
            rm_iso(r->segs[k].start, iso, sizeof iso);
            jw_str(out, iso);
            jw_key(out, "end_iso");
            rm_iso(r->segs[k].end, iso, sizeof iso);
            jw_str(out, iso);
            jw_obj_end(out);
        }
        jw_arr_end(out);
        jw_obj_end(out);
    }
    jw_arr_end(out);
    jw_obj_end(out);

    /* ---- cleanup ---- */
    free(blist);
    free(tlist);
    free(rows);
    for (i = 0; i < budgets.cap; i++) {
        RmPairBudget *pb = budgets.ents[i].key
            ? (RmPairBudget *)budgets.ents[i].val : NULL;
        if (pb) rm_pair_budget_free(pb);
    }
    map_free(&budgets);
    for (i = 0; i < anchors.cap; i++) {
        RmAnchorGroup *g = anchors.ents[i].key
            ? (RmAnchorGroup *)anchors.ents[i].val : NULL;
        if (!g) continue;
        free(g->uid);
        free(g->model);
        rm_anchor_list_free(&g->list);
        free(g);
    }
    map_free(&anchors);
    map_free(&active);
    for (i = 0; i < totals.cap; i++) {
        RmTotal *t = totals.ents[i].key
            ? (RmTotal *)totals.ents[i].val : NULL;
        free(t);
    }
    map_free(&totals);
    map_free(&realm_of);
    free(accts);
}
