/* One usage-log row, read out of its parsed JSON object. */
#ifndef WBC_ROW_H
#define WBC_ROW_H

#include "wbc_json.h"
#include "wbc_util.h"

typedef struct {
    int has_at;
    double at;
    const char *model;      /* NULL when the field is absent or null */
    const char *account;
    const char *realm;      /* the row's own realm field */
    int has_key;            /* the "key" field exists at all */
    const char *key;        /* its value; may be NULL/empty */
    const char *outcome;    /* NULL when absent */
    int error_truthy;       /* "error" present and truthy */
    double prompt, completion, reasoning, cached, total, credit;
    int prompt_i, completion_i, reasoning_i, cached_i, total_i, credit_i;
    /* timing values for the analytics accumulators (falsy reads as 0) */
    double ttft, speed, elapsed;
    /* perf values: gen_ms and cache_hit_pct, plus "present and numeric"
     * flags for the fields the perf fold tests with `is not None`
     * (ttft_ms, gen_ms, cache_hit_pct). */
    double gen, hit;
    int has_ttft, has_gen, has_hit;
    const char *cost_policy;
    const char *iso;
} Row;

/* Fill `r` from a parsed JSON object (must be jis_obj). */
void row_from_json(Row *r, const JVal *obj);

/* Copy every string field into the Row's own storage so the parsed tree can
 * be freed immediately (the streaming reader keeps no trees). */
void row_own(Row *r);
void row_free(Row *r);

/* Python row_outcome(): the outcome field, else failed/completed by error. */
const char *row_outcome_of(const Row *r);

/* Python row_realm(): the realm field, else the account's realm from the
 * optional accounts map (uid -> realm string), else detect_model_realm()
 * ported from wb_proxy.py, else default_realm. */
const char *row_realm_of(const Row *r, const Map *accounts,
                         const char *default_realm);

#endif
