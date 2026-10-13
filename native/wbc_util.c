#define _POSIX_C_SOURCE 200809L

#include "wbc_util.h"

#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void buf_init(Buf *b)
{
    b->data = NULL;
    b->len = b->cap = 0;
}

void buf_free(Buf *b)
{
    free(b->data);
    b->data = NULL;
    b->len = b->cap = 0;
}

void buf_reserve(Buf *b, size_t extra)
{
    size_t need = b->len + extra + 1;
    size_t cap;
    char *grown;
    if (need <= b->cap) return;
    cap = b->cap ? b->cap : 256;
    while (cap < need) cap *= 2;
    grown = (char *)realloc(b->data, cap);
    if (!grown) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    b->data = grown;
    b->cap = cap;
}

void buf_put(Buf *b, const void *p, size_t n)
{
    buf_reserve(b, n);
    memcpy(b->data + b->len, p, n);
    b->len += n;
    b->data[b->len] = '\0';
}

void buf_puts(Buf *b, const char *s)
{
    buf_put(b, s, strlen(s));
}

void buf_putc(Buf *b, char c)
{
    buf_reserve(b, 1);
    b->data[b->len++] = c;
    b->data[b->len] = '\0';
}

void buf_printf(Buf *b, const char *fmt, ...)
{
    va_list ap;
    int need;
    va_start(ap, fmt);
    need = vsnprintf(NULL, 0, fmt, ap);
    va_end(ap);
    if (need < 0) return;
    buf_reserve(b, (size_t)need);
    va_start(ap, fmt);
    vsnprintf(b->data + b->len, (size_t)need + 1, fmt, ap);
    va_end(ap);
    b->len += (size_t)need;
}

/* FNV-1a */
static uint64_t map_hash(const char *key)
{
    uint64_t hash = 1469598103934665603ULL;
    const unsigned char *p = (const unsigned char *)key;
    while (*p) {
        hash ^= *p++;
        hash *= 1099511628211ULL;
    }
    return hash;
}

static void map_rehash(Map *m, size_t cap)
{
    MapEnt *old = m->ents;
    size_t old_cap = m->cap, i;
    m->ents = (MapEnt *)calloc(cap, sizeof(MapEnt));
    if (!m->ents) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    m->cap = cap;
    m->len = 0;
    for (i = 0; i < old_cap; i++) {
        if (old[i].key) map_put(m, old[i].key, old[i].val);
    }
    free(old);
}

void map_init(Map *m)
{
    m->ents = NULL;
    m->cap = m->len = 0;
}

void map_free(Map *m)
{
    size_t i;
    for (i = 0; i < m->cap; i++) free(m->ents[i].key);
    free(m->ents);
    m->ents = NULL;
    m->cap = m->len = 0;
}

static MapEnt *map_slot(const Map *m, const char *key)
{
    size_t mask = m->cap - 1;
    size_t i = (size_t)map_hash(key) & mask;
    while (m->ents[i].key) {
        if (!strcmp(m->ents[i].key, key)) return &m->ents[i];
        i = (i + 1) & mask;
    }
    return &m->ents[i];
}

void *map_get(const Map *m, const char *key)
{
    MapEnt *slot;
    if (!m->cap) return NULL;
    slot = map_slot(m, key);
    return slot->key ? slot->val : NULL;
}

void *map_put(Map *m, const char *key, void *val)
{
    MapEnt *slot;
    void *old = NULL;
    if (!m->cap) map_rehash(m, 16);
    if ((m->len + 1) * 4 >= m->cap * 3) map_rehash(m, m->cap * 2);
    slot = map_slot(m, key);
    if (slot->key) {
        old = slot->val;
        slot->val = val;
        return old;
    }
    slot->key = strdup(key);
    if (!slot->key) {
        fprintf(stderr, "wb-agg: out of memory\n");
        exit(3);
    }
    slot->val = val;
    m->len++;
    return NULL;
}

void *map_get_or_put(Map *m, const char *key, void *val, int *created)
{
    void *found = map_get(m, key);
    if (found) {
        if (created) *created = 0;
        return found;
    }
    map_put(m, key, val);
    if (created) *created = 1;
    return val;
}

void num_add(Num *n, double value, int is_int)
{
    if (is_int && !n->has_float) {
        n->isum += (int64_t)value;
        return;
    }
    if (!n->has_float) {
        n->has_float = 1;
        n->fsum = (double)n->isum;
        n->isum = 0;
    }
    n->fsum += value;
}

double num_value(const Num *n)
{
    return n->has_float ? n->fsum : (double)n->isum;
}

/* Python's round() rounds the exact binary value to the nearest decimal with
 * ties to even.  Scaling by 10 first (x*10 -> half-even -> /10) is NOT the
 * same: 5296.85 (a double slightly above the decimal tie) scaled by 10 lands
 * exactly on the tie and rounds the wrong way.  printf's %.*f performs the
 * same correctly-rounded decimal conversion, so go through it. */
double py_round_half_even(double x)
{
    char buf[512];
    if (!(x > -1e300 && x < 1e300)) return x;   /* inf/nan: leave alone */
    snprintf(buf, sizeof(buf), "%.0f", x);
    return strtod(buf, NULL);
}

double py_round1(double x)
{
    char buf[512];
    if (!(x > -1e300 && x < 1e300)) return x;
    snprintf(buf, sizeof(buf), "%.1f", x);
    return strtod(buf, NULL);
}
