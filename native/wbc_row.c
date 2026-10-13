#include "wbc_row.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Python truthiness for the value shapes a usage row carries. */
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

/* row.get(field) or 0: a falsy value reads as integer 0. */
static void read_num(const JVal *obj, const char *field, double *value, int *is_int)
{
    const JVal *v = jget(obj, field);
    if (!val_truthy(v)) {
        *value = 0.0;
        *is_int = 1;
        return;
    }
    *value = jnum(v);
    *is_int = jnum_is_int(v);
}

/* Present and numeric (not null, not a non-number): the shape the perf fold
 * reads with `is not None`.  A non-numeric value is treated as absent (real
 * logs carry numbers; documented divergence). */
static void read_opt_num(const JVal *obj, const char *field, double *value, int *has)
{
    const JVal *v = jget(obj, field);
    if (v && jis_num(v)) {
        *value = jnum(v);
        *has = 1;
    } else {
        *value = 0.0;
        *has = 0;
    }
}

static const char *read_str(const JVal *obj, const char *field)
{
    const JVal *v = jget(obj, field);
    if (!v || !jis_str(v)) return NULL;
    return jstr(v);
}

void row_from_json(Row *r, const JVal *obj)
{
    const JVal *at = jget(obj, "at");
    memset(r, 0, sizeof(*r));
    r->has_at = at && jis_num(at);
    r->at = r->has_at ? jnum(at) : 0.0;
    r->model = read_str(obj, "model");
    r->account = read_str(obj, "account");
    r->realm = read_str(obj, "realm");
    r->has_key = jget(obj, "key") != NULL;
    r->key = read_str(obj, "key");
    r->outcome = read_str(obj, "outcome");
    r->error_truthy = val_truthy(jget(obj, "error"));
    read_num(obj, "prompt_tokens", &r->prompt, &r->prompt_i);
    read_num(obj, "completion_tokens", &r->completion, &r->completion_i);
    read_num(obj, "reasoning_tokens", &r->reasoning, &r->reasoning_i);
    read_num(obj, "cached_tokens", &r->cached, &r->cached_i);
    read_num(obj, "total_tokens", &r->total, &r->total_i);
    read_num(obj, "credit", &r->credit, &r->credit_i);
    {
        int ignored;
        read_num(obj, "ttft_ms", &r->ttft, &ignored);
        read_num(obj, "tokens_per_sec", &r->speed, &ignored);
        read_num(obj, "elapsed_ms", &r->elapsed, &ignored);
    }
    read_opt_num(obj, "ttft_ms", &r->ttft, &r->has_ttft);
    read_opt_num(obj, "gen_ms", &r->gen, &r->has_gen);
    read_opt_num(obj, "cache_hit_pct", &r->hit, &r->has_hit);
    r->cost_policy = read_str(obj, "cost_policy");
    r->iso = read_str(obj, "iso");
}

const char *row_outcome_of(const Row *r)
{
    if (r->outcome && r->outcome[0]) return r->outcome;
    return r->error_truthy ? "failed" : "completed";
}

/* wb_proxy.detect_model_realm, ported verbatim. */
static const char *detect_model_realm(const char *model, const char *default_realm)
{
    static const char *const intl_only[] = {
        "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gemini-3.5-flash",
        "grok-4.7", NULL
    };
    static const char *const cn_only[] = {
        "deepseek-v4-pro", "minimax-m3", "minimax-m2.7", "minimax-m2.5",
        "glm-5.1", "glm-5.0-turbo", "glm-4.6v",
        "kimi-k3-1", "kimi-k2.7", "kimi-k2-thinking",
        "hy3-x", "hy4-preview-dev", "hy4-preview-x", NULL
    };
    char lower[128];
    size_t i, n;
    if (!model || !model[0]) return default_realm;
    n = strlen(model);
    if (n >= sizeof(lower)) n = sizeof(lower) - 1;
    for (i = 0; i < n; i++) {
        char c = model[i];
        lower[i] = (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
    }
    lower[n] = '\0';
    for (i = 0; intl_only[i]; i++) {
        if (!strcmp(lower, intl_only[i])) return "intl";
    }
    if (!strncmp(lower, "gpt-", 4) || !strncmp(lower, "gemini-", 7)) return "intl";
    for (i = 0; cn_only[i]; i++) {
        if (!strcmp(lower, cn_only[i])) return "cn";
    }
    if (!strncmp(lower, "minimax-", 8) || !strncmp(lower, "deepseek-v4-pro", 15))
        return "cn";
    return default_realm;
}

const char *row_realm_of(const Row *r, const Map *accounts, const char *default_realm)
{
    if (r->realm && r->realm[0]) return r->realm;
    if (r->account && r->account[0] && accounts) {
        const char *realm = (const char *)map_get(accounts, r->account);
        if (realm && realm[0]) return realm;
    }
    if (r->model && r->model[0]) return detect_model_realm(r->model, default_realm);
    return "intl";
}

/* Row strings live in a chain of bump chunks: a malloc per string cost a
 * header each and dominated the resident set on a long log.  Nothing is freed
 * individually; the process exits with the rows. */
typedef struct RowChunk {
    struct RowChunk *next;
    size_t used, cap;
    char data[];
} RowChunk;

static RowChunk *row_chunks;

static char *dup_or_null(const char *s)
{
    size_t n, need;
    RowChunk *c = row_chunks;
    char *out;
    if (!s) return NULL;
    n = strlen(s) + 1;
    need = (n + 7u) & ~(size_t)7u;
    if (c == NULL || c->used + need > c->cap) {
        size_t cap = need > (size_t)256 * 1024 ? need : (size_t)256 * 1024;
        RowChunk *fresh = (RowChunk *)malloc(sizeof(RowChunk) + cap);
        if (!fresh) {
            fprintf(stderr, "wb-agg: out of memory\n");
            exit(3);
        }
        fresh->next = row_chunks;
        fresh->used = 0;
        fresh->cap = cap;
        row_chunks = fresh;
        c = fresh;
    }
    out = c->data + c->used;
    c->used += need;
    memcpy(out, s, n);
    return out;
}

void row_own(Row *r)
{
    r->model = dup_or_null(r->model);
    r->account = dup_or_null(r->account);
    r->realm = dup_or_null(r->realm);
    r->key = dup_or_null(r->key);
    r->outcome = dup_or_null(r->outcome);
    r->cost_policy = dup_or_null(r->cost_policy);
    r->iso = dup_or_null(r->iso);
}

void row_free(Row *r)
{
    /* the strings live in the shared arena; only the struct is cleared */
    memset(r, 0, sizeof(*r));
}
