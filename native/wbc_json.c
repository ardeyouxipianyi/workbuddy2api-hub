/* wbc_json.c -- standalone JSON parser and writer for the wb-agg engine.
 *
 * Interface: the "wbc_json.h" section of CONTRACT.md (inlined below under
 * the guard the header itself would use -- this task is restricted to two
 * .c files; see the note above the guard for how to materialize the
 * header).
 *
 * C99 only, no VLAs, no C11 features; dependencies: wbc_util.h (Buf) and
 * the C standard library.
 *
 * Parser (jparse):
 *   - recursive descent: objects / arrays / strings / numbers / true /
 *     false / null; nesting deeper than 64 is an error;
 *   - strings decode the usual escapes plus \uXXXX: a surrogate pair
 *     becomes one 4-byte UTF-8 sequence, a lone surrogate is emitted as
 *     its 3-byte WTF-8 form (the code point Python keeps).  Raw bytes
 *     below 0x20 inside a string are rejected, like Python's
 *     json.loads(strict=True); bytes >= 0x80 pass through as UTF-8.  A
 *     decoded \u0000 truncates the C string (real logs carry none);
 *   - numbers without '.', 'e' or 'E' that fit in int64 stay integers,
 *     everything else -- including integers past int64 -- becomes a
 *     double (real logs never carry those; noted in CONTRACT.md);
 *     NaN / Infinity literals are not accepted;
 *   - trailing whitespace after the value is fine, any other trailing
 *     content is an error (Python json.loads raises "Extra data");
 *   - a duplicate object key keeps its first position and receives the
 *     last value, exactly like a Python dict;
 *   - on error jparse returns NULL and jparse_err() describes the failure
 *     with a byte offset; out-of-memory is reported the same way.
 *
 * Writer (jw_*):
 *   - backed by Buf; the caller never writes commas: the writer keeps one
 *     state frame per open container, inserts "," before every item but
 *     the first, and expects exactly one value after jw_key();
 *   - jw_str / jw_key escape '"', '\\' and bytes < 0x20 (\u00XX); bytes
 *     >= 0x80 pass through untouched (UTF-8 in, UTF-8 out);
 *   - jw_double prints %.17g so the value reads back exactly; non-finite
 *     doubles print as NaN / Infinity / -Infinity -- the literals
 *     Python's json.dumps emits, so a comparison harness sees the same
 *     text;
 *   - jw_str(NULL) writes "" (never crashes on an absent string);
 *   - jw_take() hands the malloc'd buffer to the caller and empties the
 *     JW; jw_free() stays safe afterwards;
 *   - writer OOM is fatal (exit 3), matching wbc_util.c.
 *
 * Self-test: cc -std=c99 -Wall -Wextra -o tests_json tests_json.c wbc_json.c wbc_util.c
 */

#include <errno.h>
#include <float.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "wbc_util.h"

/* ---------------------------------------------------------------------
 * wbc_json.h is not written as a separate file (two-file limit on this
 * task).  The guarded block below IS the header text: to materialize
 * wbc_json.h, copy it verbatim into a file named wbc_json.h (main.c and
 * wbc_row.h include it).
 * ------------------------------------------------------------------- */
#ifndef WBC_JSON_H
#define WBC_JSON_H

typedef struct JVal JVal;

JVal *jparse(const char *buf, size_t len);   /* NULL = syntax error */
const char *jparse_err(void);                /* text of the last error */
void jfree(JVal *v);
int jis_obj(const JVal *v);
int jis_arr(const JVal *v);
int jis_str(const JVal *v);
int jis_num(const JVal *v);
int jis_null(const JVal *v);
int jis_bool(const JVal *v);
const JVal *jget(const JVal *obj, const char *key);   /* NULL if absent */
size_t jlen(const JVal *arr);
const JVal *jat(const JVal *arr, size_t i);
const char *jstr(const JVal *s);            /* UTF-8, NUL-terminated */
int jbool(const JVal *v);                   /* 1/0; 0 for non-bool */
int jnum_is_int(const JVal *v);             /* integer and inside int64 */
long long jint(const JVal *v);              /* 0 for non-integer */
double jnum(const JVal *v);                 /* 0.0 for non-number */
/* object key iteration, in document order */
size_t jobj_len(const JVal *obj);
const char *jobj_key(const JVal *obj, size_t i);
const JVal *jobj_val(const JVal *obj, size_t i);

/* ---- writer ---- */
typedef struct JW JW;
JW *jw_new(void);
void jw_free(JW *jw);
char *jw_take(JW *jw, size_t *len);         /* detaches the buffer */
void jw_raw(JW *jw, const char *s);
void jw_str(JW *jw, const char *s);         /* quotes + escapes */
void jw_int(JW *jw, long long v);
void jw_double(JW *jw, double v);           /* %.17g, exact read-back */
void jw_bool(JW *jw, int v);
void jw_null(JW *jw);
void jw_obj_begin(JW *jw);
void jw_obj_end(JW *jw);
void jw_arr_begin(JW *jw);
void jw_arr_end(JW *jw);
void jw_key(JW *jw, const char *key);       /* "key": */

#endif /* WBC_JSON_H */

/* ===================== value representation ===================== */

#define JV_NULL   0
#define JV_BOOL   1
#define JV_INT    2
#define JV_DOUBLE 3
#define JV_STR    4
#define JV_ARR    5
#define JV_OBJ    6

#define JV_MAX_DEPTH 64

typedef struct JEnt {
    char *key;
    JVal *val;
} JEnt;

/* ---- per-document arena ----
 * Every node, key and string of one parsed document lives in bump chunks that
 * are released in one shot, instead of a malloc per node and per string: on a
 * real usage row that was ~40 allocator round trips, and the parse dominated
 * the whole engine.  The root node owns the arena; jfree() releases it. */
#define JA_CHUNK ((size_t)64 * 1024)

typedef struct JChunk {
    struct JChunk *next;
    size_t used, cap;
    char *data;
} JChunk;

typedef struct JArena {
    JChunk *head;
} JArena;

static void *arena_alloc(JArena *a, size_t n)
{
    JChunk *c = a->head;
    void *p;
    n = (n + 7u) & ~(size_t)7u;
    if (c == NULL || c->used + n > c->cap) {
        size_t cap = n > JA_CHUNK ? n : JA_CHUNK;
        JChunk *fresh = (JChunk *)malloc(sizeof(JChunk));
        if (fresh == NULL) return NULL;
        fresh->data = (char *)malloc(cap);
        if (fresh->data == NULL) {
            free(fresh);
            return NULL;
        }
        fresh->cap = cap;
        fresh->used = 0;
        fresh->next = a->head;
        a->head = fresh;
        c = fresh;
    }
    p = c->data + c->used;
    c->used += n;
    memset(p, 0, n);
    return p;
}

static void arena_free(JArena *a)
{
    JChunk *c = a->head;
    while (c != NULL) {
        JChunk *next = c->next;
        free(c->data);
        free(c);
        c = next;
    }
    a->head = NULL;
}

/* Same bump, without the zero-fill: callers that write every byte of the
 * block (strings, member arrays) do not need it. */
static void *arena_alloc_raw(JArena *a, size_t n)
{
    JChunk *c = a->head;
    void *p;
    n = (n + 7u) & ~(size_t)7u;
    if (c == NULL || c->used + n > c->cap) {
        size_t cap = n > JA_CHUNK ? n : JA_CHUNK;
        JChunk *fresh = (JChunk *)malloc(sizeof(JChunk));
        if (fresh == NULL) return NULL;
        fresh->data = (char *)malloc(cap);
        if (fresh->data == NULL) {
            free(fresh);
            return NULL;
        }
        fresh->cap = cap;
        fresh->used = 0;
        fresh->next = a->head;
        a->head = fresh;
        c = fresh;
    }
    p = c->data + c->used;
    c->used += n;
    return p;
}

struct JVal {
    int type;
    JArena *arena;                 /* set on the root only */
    union {
        int b;
        long long i;
        double d;
        char *s;
        struct {
            JVal **items;
            size_t len, cap;
        } arr;
        struct {
            JEnt *ents;
            size_t len, cap;
        } obj;
    } u;
};

static JVal *jv_new(JArena *a, int type)
{
    JVal *v = (JVal *)arena_alloc(a, sizeof(JVal));
    if (v == NULL) return NULL;
    v->type = type;
    return v;
}

void jfree(JVal *v)
{
    size_t i;
    if (v == NULL) return;
    if (v->arena != NULL) {
        JArena *arena = v->arena;
        arena_free(arena);
        free(arena);
        return;
    }
    /* Legacy path for values that were not arena-built (none are today). */
    if (v->type == JV_STR) {
        free(v->u.s);
    } else if (v->type == JV_ARR) {
        for (i = 0; i < v->u.arr.len; i++) jfree(v->u.arr.items[i]);
        free(v->u.arr.items);
    } else if (v->type == JV_OBJ) {
        for (i = 0; i < v->u.obj.len; i++) {
            free(v->u.obj.ents[i].key);
            jfree(v->u.obj.ents[i].val);
        }
        free(v->u.obj.ents);
    }
    free(v);
}

/* Append to an array; returns 0, or -1 on out of memory. */
static int jarr_push(JArena *a, JVal *arr, JVal *item)
{
    if (arr->u.arr.len == arr->u.arr.cap) {
        size_t ncap = arr->u.arr.cap ? arr->u.arr.cap * 2 : 8;
        JVal **grown = (JVal **)arena_alloc_raw(a, ncap * sizeof(JVal *));
        if (grown == NULL) return -1;
        if (arr->u.arr.items != NULL)
            memcpy(grown, arr->u.arr.items, arr->u.arr.len * sizeof(JVal *));
        arr->u.arr.items = grown;
        arr->u.arr.cap = ncap;
    }
    arr->u.arr.items[arr->u.arr.len++] = item;
    return 0;
}

/* Append key/val to an object (taking ownership); a duplicate key keeps its
 * first position and receives the new value, like a Python dict.  Returns
 * 0, or -1 on out of memory -- nothing is consumed then. */
static int jobj_push(JArena *a, JVal *obj, char *key, JVal *val)
{
    size_t i;
    char first = key[0];
    for (i = 0; i < obj->u.obj.len; i++) {
        const char *have = obj->u.obj.ents[i].key;
        if (have[0] == first && strcmp(have, key) == 0) {
            obj->u.obj.ents[i].val = val;
            return 0;
        }
    }
    if (obj->u.obj.len == obj->u.obj.cap) {
        size_t ncap = obj->u.obj.cap ? obj->u.obj.cap * 2 : 8;
        JEnt *grown = (JEnt *)arena_alloc_raw(a, ncap * sizeof(JEnt));
        if (grown == NULL) return -1;
        if (obj->u.obj.ents != NULL)
            memcpy(grown, obj->u.obj.ents, obj->u.obj.len * sizeof(JEnt));
        obj->u.obj.ents = grown;
        obj->u.obj.cap = ncap;
    }
    obj->u.obj.ents[obj->u.obj.len].key = key;
    obj->u.obj.ents[obj->u.obj.len].val = val;
    obj->u.obj.len++;
    return 0;
}

/* ===================== accessors (NULL-safe) ===================== */

int jis_obj(const JVal *v) { return v != NULL && v->type == JV_OBJ; }
int jis_arr(const JVal *v) { return v != NULL && v->type == JV_ARR; }
int jis_str(const JVal *v) { return v != NULL && v->type == JV_STR; }
int jis_null(const JVal *v) { return v != NULL && v->type == JV_NULL; }
int jis_bool(const JVal *v) { return v != NULL && v->type == JV_BOOL; }

int jis_num(const JVal *v)
{
    return v != NULL && (v->type == JV_INT || v->type == JV_DOUBLE);
}

const JVal *jget(const JVal *obj, const char *key)
{
    size_t i;
    char first;
    if (obj == NULL || obj->type != JV_OBJ || key == NULL) return NULL;
    first = key[0];
    for (i = 0; i < obj->u.obj.len; i++) {
        const char *have = obj->u.obj.ents[i].key;
        if (have[0] == first && strcmp(have, key) == 0)
            return obj->u.obj.ents[i].val;
    }
    return NULL;
}

size_t jlen(const JVal *v)
{
    return (v != NULL && v->type == JV_ARR) ? v->u.arr.len : 0;
}

const JVal *jat(const JVal *v, size_t i)
{
    if (v == NULL || v->type != JV_ARR || i >= v->u.arr.len) return NULL;
    return v->u.arr.items[i];
}

const char *jstr(const JVal *v)
{
    return (v != NULL && v->type == JV_STR) ? v->u.s : NULL;
}

int jbool(const JVal *v)
{
    return (v != NULL && v->type == JV_BOOL) ? v->u.b : 0;
}

int jnum_is_int(const JVal *v)
{
    return v != NULL && v->type == JV_INT;
}

long long jint(const JVal *v)
{
    return (v != NULL && v->type == JV_INT) ? v->u.i : 0;
}

double jnum(const JVal *v)
{
    if (v == NULL) return 0.0;
    if (v->type == JV_INT) return (double)v->u.i;
    if (v->type == JV_DOUBLE) return v->u.d;
    return 0.0;
}

size_t jobj_len(const JVal *v)
{
    return (v != NULL && v->type == JV_OBJ) ? v->u.obj.len : 0;
}

const char *jobj_key(const JVal *v, size_t i)
{
    if (v == NULL || v->type != JV_OBJ || i >= v->u.obj.len) return NULL;
    return v->u.obj.ents[i].key;
}

const JVal *jobj_val(const JVal *v, size_t i)
{
    if (v == NULL || v->type != JV_OBJ || i >= v->u.obj.len) return NULL;
    return v->u.obj.ents[i].val;
}

/* ========================= parser ========================= */

static char jerr_buf[200] = "no error";

typedef struct {
    const char *s;
    size_t len;
    size_t pos;
    int depth;
    int err;
    JArena *arena;
} JParser;

static void jset_err(JParser *p, const char *msg)
{
    if (p->err) return;
    p->err = 1;
    snprintf(jerr_buf, sizeof(jerr_buf), "wbc_json: %s (byte %lu)",
             msg, (unsigned long)p->pos);
}

static int jis_digit(char c) { return c >= '0' && c <= '9'; }
static int jis_ws(char c)
{
    return c == ' ' || c == '\t' || c == '\n' || c == '\r';
}

static void jskip_ws(JParser *p)
{
    while (p->pos < p->len && jis_ws(p->s[p->pos])) p->pos++;
}

static int jhexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Read four hex digits at p->pos; advances only on success. */
static int jhex4(JParser *p)
{
    int v = 0;
    size_t i;
    if (p->len - p->pos < 4) return -1;
    for (i = 0; i < 4; i++) {
        int d = jhexval(p->s[p->pos + i]);
        if (d < 0) return -1;
        v = (v << 4) | d;
    }
    p->pos += 4;
    return v;
}

static void jutf8_put(Buf *b, unsigned long cp)
{
    if (cp < 0x80UL) {
        buf_putc(b, (char)cp);
    } else if (cp < 0x800UL) {
        buf_putc(b, (char)(0xC0UL | (cp >> 6)));
        buf_putc(b, (char)(0x80UL | (cp & 0x3FUL)));
    } else if (cp < 0x10000UL) {
        buf_putc(b, (char)(0xE0UL | (cp >> 12)));
        buf_putc(b, (char)(0x80UL | ((cp >> 6) & 0x3FUL)));
        buf_putc(b, (char)(0x80UL | (cp & 0x3FUL)));
    } else {
        buf_putc(b, (char)(0xF0UL | (cp >> 18)));
        buf_putc(b, (char)(0x80UL | ((cp >> 12) & 0x3FUL)));
        buf_putc(b, (char)(0x80UL | ((cp >> 6) & 0x3FUL)));
        buf_putc(b, (char)(0x80UL | (cp & 0x3FUL)));
    }
}

static JVal *jparse_value(JParser *p);
static JVal *jparse_object(JParser *p);
static JVal *jparse_array(JParser *p);

/* Parse a string starting at the opening quote; returns an arena-allocated
 * NUL-terminated UTF-8 string, or NULL with p->err set.
 *
 * A string with no escape at all — nearly every string in a real usage row —
 * is copied straight from the input in one memcpy.  Escaped strings fall back
 * to the Buf path and are copied into the arena at the end. */
static char *jparse_string(JParser *p)
{
    Buf b;
    size_t start, run;
    int escaped = 0;
    buf_init(&b);
    start = p->pos + 1;                        /* first byte after the quote */
    run = start;
    while (run < p->len) {
        unsigned char c = (unsigned char)p->s[run];
        if (c == '"') break;
        if (c == '\\') { escaped = 1; break; }
        if (c < 0x20) {
            jset_err(p, "raw control character in string");
            buf_free(&b);
            return NULL;
        }
        run++;
    }
    if (!escaped && run < p->len) {
        size_t n = run - start;
        char *out = (char *)arena_alloc_raw(p->arena, n + 1);
        if (out == NULL) {
            jset_err(p, "out of memory");
            buf_free(&b);
            return NULL;
        }
        memcpy(out, p->s + start, n);
        out[n] = '\0';
        p->pos = run + 1;
        return out;
    }
    p->pos = start;
    for (;;) {
        unsigned char c;
        /* copy the run of ordinary bytes up to the next quote, backslash or
         * control character */
        run = p->pos;
        while (run < p->len) {
            c = (unsigned char)p->s[run];
            if (c == '"' || c == '\\' || c < 0x20) break;
            run++;
        }
        if (run > p->pos) {
            buf_put(&b, p->s + p->pos, run - p->pos);
            p->pos = run;
        }
        if (p->pos >= p->len) {
            jset_err(p, "unterminated string");
            buf_free(&b);
            return NULL;
        }
        c = (unsigned char)p->s[p->pos++];
        if (c == '"') break;
        if (c == '\\') {
            if (p->pos >= p->len) {
                jset_err(p, "unterminated string");
                buf_free(&b);
                return NULL;
            }
            c = (unsigned char)p->s[p->pos++];
            switch (c) {
            case '"':  buf_putc(&b, '"');  break;
            case '\\': buf_putc(&b, '\\'); break;
            case '/':  buf_putc(&b, '/');  break;
            case 'b':  buf_putc(&b, '\b'); break;
            case 'f':  buf_putc(&b, '\f'); break;
            case 'n':  buf_putc(&b, '\n'); break;
            case 'r':  buf_putc(&b, '\r'); break;
            case 't':  buf_putc(&b, '\t'); break;
            case 'u': {
                int hi = jhex4(p);
                if (hi < 0) {
                    jset_err(p, "bad \\u escape");
                    buf_free(&b);
                    return NULL;
                }
                if (hi >= 0xD800 && hi <= 0xDBFF) {
                    /* a high surrogate pairs with an immediately following
                     * low surrogate; otherwise it stands alone (WTF-8) */
                    if (p->pos + 1 < p->len && p->s[p->pos] == '\\' &&
                        p->s[p->pos + 1] == 'u') {
                        size_t save = p->pos;
                        int lo;
                        p->pos += 2;
                        lo = jhex4(p);
                        if (lo >= 0xDC00 && lo <= 0xDFFF) {
                            unsigned long cp = 0x10000UL +
                                ((unsigned long)(hi - 0xD800) << 10) +
                                (unsigned long)(lo - 0xDC00);
                            jutf8_put(&b, cp);
                            break;
                        }
                        p->pos = save;  /* not a pair: leave it for the loop */
                    }
                    jutf8_put(&b, (unsigned long)hi);
                } else {
                    jutf8_put(&b, (unsigned long)hi);
                }
                break;
            }
            default:
                jset_err(p, "invalid escape");
                buf_free(&b);
                return NULL;
            }
        } else if (c < 0x20) {
            jset_err(p, "raw control character in string");
            buf_free(&b);
            return NULL;
        } else {
            buf_putc(&b, (char)c);
        }
    }
    {
        size_t n = b.len;
        char *out = (char *)arena_alloc(p->arena, n + 1);
        if (out == NULL) {
            jset_err(p, "out of memory");
            buf_free(&b);
            return NULL;
        }
        if (n > 0) memcpy(out, b.data, n);
        out[n] = '\0';
        buf_free(&b);
        return out;
    }
}

/* Parse a number: int64 when it has no fraction/exponent and fits,
 * otherwise double (including integers past int64). */
static JVal *jparse_number(JParser *p)
{
    size_t start = p->pos;
    size_t n;
    int is_float = 0;
    char stack_tok[32];
    char *heap_tok = NULL;
    const char *tok;
    JVal *v = NULL;

    if (p->s[p->pos] == '-') p->pos++;
    if (p->pos >= p->len) {
        jset_err(p, "invalid number");
        return NULL;
    }
    if (p->s[p->pos] == '0') {
        p->pos++;
        if (p->pos < p->len && jis_digit(p->s[p->pos])) {
            jset_err(p, "invalid number (leading zero)");
            return NULL;
        }
    } else if (jis_digit(p->s[p->pos])) {
        while (p->pos < p->len && jis_digit(p->s[p->pos])) p->pos++;
    } else {
        jset_err(p, "invalid number");
        return NULL;
    }
    if (p->pos < p->len && p->s[p->pos] == '.') {
        is_float = 1;
        p->pos++;
        if (p->pos >= p->len || !jis_digit(p->s[p->pos])) {
            jset_err(p, "invalid number (fraction)");
            return NULL;
        }
        while (p->pos < p->len && jis_digit(p->s[p->pos])) p->pos++;
    }
    if (p->pos < p->len && (p->s[p->pos] == 'e' || p->s[p->pos] == 'E')) {
        is_float = 1;
        p->pos++;
        if (p->pos < p->len && (p->s[p->pos] == '+' || p->s[p->pos] == '-'))
            p->pos++;
        if (p->pos >= p->len || !jis_digit(p->s[p->pos])) {
            jset_err(p, "invalid number (exponent)");
            return NULL;
        }
        while (p->pos < p->len && jis_digit(p->s[p->pos])) p->pos++;
    }
    n = p->pos - start;
    if (n < sizeof(stack_tok)) {
        memcpy(stack_tok, p->s + start, n);
        stack_tok[n] = '\0';
        tok = stack_tok;
    } else {
        heap_tok = (char *)malloc(n + 1);
        if (heap_tok == NULL) {
            jset_err(p, "out of memory");
            return NULL;
        }
        memcpy(heap_tok, p->s + start, n);
        heap_tok[n] = '\0';
        tok = heap_tok;
    }
    if (!is_float && n <= 20) {
        /* Hand-rolled integer: the grammar above guarantees digits with an
         * optional leading '-', and strtoll per number was a measurable
         * share of the parse on a real row.  Overflow falls through to the
         * double path, exactly like strtoll's ERANGE did. */
        unsigned long long mag = 0;
        int neg = 0;
        int overflow = 0;
        size_t i = 0;
        if (tok[0] == '-') { neg = 1; i = 1; }
        for (; i < n; i++) {
            unsigned int d = (unsigned int)(tok[i] - '0');
            if (mag > (18446744073709551615ULL - d) / 10ULL) {
                overflow = 1;
                break;
            }
            mag = mag * 10ULL + d;
        }
        if (!overflow) {
            if ((!neg && mag <= 9223372036854775807ULL) ||
                (neg && mag <= 9223372036854775808ULL)) {
                v = jv_new(p->arena, JV_INT);
                if (v != NULL) {
                    if (neg && mag == 9223372036854775808ULL)
                        v->u.i = (-9223372036854775807LL - 1);
                    else
                        v->u.i = neg ? -(long long)mag : (long long)mag;
                }
            }
        }
    }
    if (v == NULL) {
        double dv = strtod(tok, NULL);
        v = jv_new(p->arena, JV_DOUBLE);
        if (v != NULL) v->u.d = dv;
    }
    if (v == NULL) jset_err(p, "out of memory");
    free(heap_tok);
    return v;
}

static JVal *jparse_literal(JParser *p, const char *lit, int type, int b)
{
    size_t n = strlen(lit);
    JVal *v;
    if (p->len - p->pos < n || memcmp(p->s + p->pos, lit, n) != 0) {
        jset_err(p, "invalid literal");
        return NULL;
    }
    p->pos += n;
    v = jv_new(p->arena, type);
    if (v == NULL) {
        jset_err(p, "out of memory");
        return NULL;
    }
    v->u.b = b;
    return v;
}

static JVal *jparse_value(JParser *p)
{
    char c;
    if (p->err) return NULL;
    if (p->pos >= p->len) {
        jset_err(p, "unexpected end of input");
        return NULL;
    }
    c = p->s[p->pos];
    if (c == '{') return jparse_object(p);
    if (c == '[') return jparse_array(p);
    if (c == '"') {
        char *s = jparse_string(p);
        JVal *v;
        if (s == NULL) return NULL;
        v = jv_new(p->arena, JV_STR);
        if (v == NULL) {
            jset_err(p, "out of memory");
            return NULL;
        }
        v->u.s = s;
        return v;
    }
    if (c == 't') return jparse_literal(p, "true", JV_BOOL, 1);
    if (c == 'f') return jparse_literal(p, "false", JV_BOOL, 0);
    if (c == 'n') return jparse_literal(p, "null", JV_NULL, 0);
    if (c == '-' || jis_digit(c)) return jparse_number(p);
    jset_err(p, "unexpected character");
    return NULL;
}

static JVal *jparse_object(JParser *p)
{
    JVal *v;
    if (p->depth >= JV_MAX_DEPTH) {
        jset_err(p, "nesting deeper than 64");
        return NULL;
    }
    p->depth++;
    v = jv_new(p->arena, JV_OBJ);
    if (v == NULL) {
        jset_err(p, "out of memory");
        p->depth--;
        return NULL;
    }
    p->pos++;                                  /* '{' */
    jskip_ws(p);
    if (p->pos < p->len && p->s[p->pos] == '}') {
        p->pos++;
        p->depth--;
        return v;
    }
    for (;;) {
        char *key;
        JVal *val;
        if (p->pos >= p->len) {
            jset_err(p, "unexpected end of input in object");
            goto fail;
        }
        if (p->s[p->pos] != '"') {
            jset_err(p, "expected string key");
            goto fail;
        }
        key = jparse_string(p);
        if (key == NULL) goto fail;
        jskip_ws(p);
        if (p->pos >= p->len || p->s[p->pos] != ':') {
            jset_err(p, "expected ':'");
            goto fail;
        }
        p->pos++;
        jskip_ws(p);
        val = jparse_value(p);
        if (val == NULL) goto fail;
        if (jobj_push(p->arena, v, key, val) != 0) {
            jset_err(p, "out of memory");
            goto fail;
        }
        jskip_ws(p);
        if (p->pos < p->len && p->s[p->pos] == ',') {
            p->pos++;
            jskip_ws(p);
            continue;
        }
        if (p->pos < p->len && p->s[p->pos] == '}') {
            p->pos++;
            break;
        }
        jset_err(p, "expected ',' or '}'");
        goto fail;
    }
    p->depth--;
    return v;
fail:
    p->depth--;
    return NULL;
}

static JVal *jparse_array(JParser *p)
{
    JVal *v;
    if (p->depth >= JV_MAX_DEPTH) {
        jset_err(p, "nesting deeper than 64");
        return NULL;
    }
    p->depth++;
    v = jv_new(p->arena, JV_ARR);
    if (v == NULL) {
        jset_err(p, "out of memory");
        p->depth--;
        return NULL;
    }
    p->pos++;                                  /* '[' */
    jskip_ws(p);
    if (p->pos < p->len && p->s[p->pos] == ']') {
        p->pos++;
        p->depth--;
        return v;
    }
    for (;;) {
        JVal *item;
        if (p->pos >= p->len) {
            jset_err(p, "unexpected end of input in array");
            goto fail;
        }
        item = jparse_value(p);
        if (item == NULL) goto fail;
        if (jarr_push(p->arena, v, item) != 0) {
            jset_err(p, "out of memory");
            goto fail;
        }
        jskip_ws(p);
        if (p->pos < p->len && p->s[p->pos] == ',') {
            p->pos++;
            jskip_ws(p);
            continue;
        }
        if (p->pos < p->len && p->s[p->pos] == ']') {
            p->pos++;
            break;
        }
        jset_err(p, "expected ',' or ']'");
        goto fail;
    }
    p->depth--;
    return v;
fail:
    p->depth--;
    return NULL;
}

JVal *jparse(const char *buf, size_t len)
{
    JParser p;
    JVal *v;
    JArena *arena;

    strcpy(jerr_buf, "no error");
    if (buf == NULL) {
        strcpy(jerr_buf, "wbc_json: null input");
        return NULL;
    }
    arena = (JArena *)malloc(sizeof(JArena));
    if (arena == NULL) {
        strcpy(jerr_buf, "wbc_json: out of memory");
        return NULL;
    }
    arena->head = NULL;
    p.s = buf;
    p.len = len;
    p.pos = 0;
    p.depth = 0;
    p.err = 0;
    p.arena = arena;
    jskip_ws(&p);
    v = jparse_value(&p);
    if (v == NULL) {
        arena_free(arena);
        free(arena);
        return NULL;
    }
    jskip_ws(&p);
    if (p.pos != p.len) {
        jset_err(&p, "trailing content after JSON value");
        arena_free(arena);
        free(arena);
        return NULL;
    }
    v->arena = arena;               /* the root owns the document's arena */
    return v;
}

const char *jparse_err(void)
{
    return jerr_buf;
}

/* ========================= writer ========================= */

typedef struct {
    size_t items;        /* values written at this level */
    int pending_key;     /* a key was written, its value is due */
    int is_obj;
} JWLevel;

struct JW {
    Buf buf;
    JWLevel *levels;
    size_t depth;        /* levels[0] is the implicit top level */
    size_t cap;
};

static void jw_oom(void)
{
    fprintf(stderr, "wb-agg: out of memory\n");
    exit(3);
}

JW *jw_new(void)
{
    JW *jw = (JW *)malloc(sizeof(JW));
    if (jw == NULL) jw_oom();
    buf_init(&jw->buf);
    jw->cap = 16;
    jw->levels = (JWLevel *)malloc(jw->cap * sizeof(JWLevel));
    if (jw->levels == NULL) jw_oom();
    jw->depth = 1;
    jw->levels[0].items = 0;
    jw->levels[0].pending_key = 0;
    jw->levels[0].is_obj = 0;
    return jw;
}

void jw_free(JW *jw)
{
    if (jw == NULL) return;
    free(jw->levels);
    buf_free(&jw->buf);
    free(jw);
}

static void jw_push(JW *jw, int is_obj)
{
    if (jw->depth == jw->cap) {
        size_t ncap = jw->cap * 2;
        JWLevel *grown = (JWLevel *)realloc(jw->levels, ncap * sizeof(JWLevel));
        if (grown == NULL) jw_oom();
        jw->levels = grown;
        jw->cap = ncap;
    }
    jw->levels[jw->depth].items = 0;
    jw->levels[jw->depth].pending_key = 0;
    jw->levels[jw->depth].is_obj = is_obj;
    jw->depth++;
}

/* Start a value at the current level: no comma right after a key, one
 * comma before every later item. */
static void jw_begin_value(JW *jw)
{
    JWLevel *lv = &jw->levels[jw->depth - 1];
    if (lv->pending_key) {
        lv->pending_key = 0;
    } else if (lv->items > 0) {
        buf_putc(&jw->buf, ',');
    }
    lv->items++;
}

static void jw_escape(JW *jw, const char *s)
{
    const unsigned char *p = (const unsigned char *)s;
    buf_putc(&jw->buf, '"');
    for (; *p; p++) {
        unsigned char c = *p;
        switch (c) {
        case '"':  buf_puts(&jw->buf, "\\\""); break;
        case '\\': buf_puts(&jw->buf, "\\\\"); break;
        case '\b': buf_puts(&jw->buf, "\\b");  break;
        case '\f': buf_puts(&jw->buf, "\\f");  break;
        case '\n': buf_puts(&jw->buf, "\\n");  break;
        case '\r': buf_puts(&jw->buf, "\\r");  break;
        case '\t': buf_puts(&jw->buf, "\\t");  break;
        default:
            if (c < 0x20) buf_printf(&jw->buf, "\\u%04x", (unsigned int)c);
            else buf_putc(&jw->buf, (char)c);
            break;
        }
    }
    buf_putc(&jw->buf, '"');
}

char *jw_take(JW *jw, size_t *len)
{
    char *out;
    if (jw == NULL) {
        if (len != NULL) *len = 0;
        return NULL;
    }
    if (jw->buf.data == NULL) {
        jw->buf.data = (char *)malloc(1);
        if (jw->buf.data == NULL) jw_oom();
        jw->buf.data[0] = '\0';
    }
    out = jw->buf.data;
    if (len != NULL) *len = jw->buf.len;
    jw->buf.data = NULL;
    jw->buf.len = 0;
    jw->buf.cap = 0;
    return out;
}

void jw_raw(JW *jw, const char *s)
{
    if (jw == NULL) return;
    jw_begin_value(jw);
    if (s != NULL) buf_puts(&jw->buf, s);
}

void jw_str(JW *jw, const char *s)
{
    if (jw == NULL) return;
    jw_begin_value(jw);
    jw_escape(jw, s != NULL ? s : "");
}

void jw_int(JW *jw, long long v)
{
    if (jw == NULL) return;
    jw_begin_value(jw);
    buf_printf(&jw->buf, "%lld", v);
}

void jw_double(JW *jw, double v)
{
    if (jw == NULL) return;
    jw_begin_value(jw);
    if (v != v) {                              /* NaN */
        buf_puts(&jw->buf, "NaN");
    } else if (v > DBL_MAX || v < -DBL_MAX) {  /* +-Infinity */
        buf_puts(&jw->buf, v < 0.0 ? "-Infinity" : "Infinity");
    } else {
        buf_printf(&jw->buf, "%.17g", v);
    }
}

void jw_bool(JW *jw, int v)
{
    if (jw == NULL) return;
    jw_begin_value(jw);
    buf_puts(&jw->buf, v ? "true" : "false");
}

void jw_null(JW *jw)
{
    if (jw == NULL) return;
    jw_begin_value(jw);
    buf_puts(&jw->buf, "null");
}

void jw_obj_begin(JW *jw)
{
    if (jw == NULL) return;
    jw_begin_value(jw);
    buf_putc(&jw->buf, '{');
    jw_push(jw, 1);
}

void jw_obj_end(JW *jw)
{
    if (jw == NULL) return;
    if (jw->depth <= 1) return;                /* no open container */
    jw->depth--;
    buf_putc(&jw->buf, '}');
}

void jw_arr_begin(JW *jw)
{
    if (jw == NULL) return;
    jw_begin_value(jw);
    buf_putc(&jw->buf, '[');
    jw_push(jw, 0);
}

void jw_arr_end(JW *jw)
{
    if (jw == NULL) return;
    if (jw->depth <= 1) return;
    jw->depth--;
    buf_putc(&jw->buf, ']');
}

void jw_key(JW *jw, const char *key)
{
    JWLevel *lv;
    if (jw == NULL) return;
    lv = &jw->levels[jw->depth - 1];
    if (lv->items > 0) buf_putc(&jw->buf, ',');
    jw_escape(jw, key != NULL ? key : "");
    buf_putc(&jw->buf, ':');
    lv->pending_key = 1;
}
