/* Shared containers for the standalone C aggregation engine. */
#ifndef WBC_UTIL_H
#define WBC_UTIL_H

#include <stddef.h>
#include <stdint.h>

/* ---- growable byte buffer ---- */
typedef struct {
    char *data;
    size_t len, cap;
} Buf;

void buf_init(Buf *b);
void buf_free(Buf *b);
void buf_reserve(Buf *b, size_t extra);
void buf_put(Buf *b, const void *p, size_t n);
void buf_puts(Buf *b, const char *s);
void buf_putc(Buf *b, char c);
void buf_printf(Buf *b, const char *fmt, ...);

/* ---- string-keyed hash map (open addressing, FNV-1a) ----
 * Keys are copied; values are owned by the caller.  A NULL value marks an
 * empty slot, so stored values must never be NULL. */
typedef struct {
    char *key;
    void *val;
} MapEnt;

typedef struct {
    MapEnt *ents;
    size_t cap, len;
} Map;

void map_init(Map *m);
void map_free(Map *m);                    /* frees keys, not values */
void *map_get(const Map *m, const char *key);
/* Insert; returns the previous value or NULL.  Copies the key. */
void *map_put(Map *m, const char *key, void *val);
/* Get-or-create in one lookup; *created is set to 1 when `val` was stored. */
void *map_get_or_put(Map *m, const char *key, void *val, int *created);

/* ---- int64 accumulation that stays exact for integer rows ----
 * Python sums ints exactly and turns the total into a float only when a
 * non-integer contribution appears.  Mirror that: accumulate integers in
 * int64 and non-integers in a double; the value is exact whenever has_float
 * stays 0 (the normal case for real logs). */
typedef struct {
    int64_t isum;
    double fsum;
    int has_float;
} Num;

void num_add(Num *n, double value, int is_int);
double num_value(const Num *n);

/* Python rounding: round() is half-to-even, and round(x, 1) rounds on the
 * one-decimal grid the same way. */
double py_round_half_even(double x);
double py_round1(double x);

#endif
