/* tests_json.c -- self-test main for wbc_json.c (parser + writer).
 *
 * Build: cc -std=c99 -Wall -Wextra -o tests_json tests_json.c wbc_json.c wbc_util.c
 * Run:   ./tests_json   -> prints "JSON SELFTEST OK <n>" and exits 0;
 *                         any failed check is reported on stderr.
 *
 * Coverage: nested documents, escapes, surrogate pairs, int64 overflow to
 * double, trailing-garbage rejection, key order, writer round-trips,
 * empty object/array, negative numbers, exponents, NaN not accepted,
 * depth limit (64 ok / 65 rejected), NULL-safe accessors, length-based
 * parsing of non-NUL-terminated buffers.
 */

#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Same interface block as wbc_json.c (the wbc_json.h text, inlined under
 * the guard the header would use). */
#ifndef WBC_JSON_H
#define WBC_JSON_H

typedef struct JVal JVal;

JVal *jparse(const char *buf, size_t len);
const char *jparse_err(void);
void jfree(JVal *v);
int jis_obj(const JVal *v);
int jis_arr(const JVal *v);
int jis_str(const JVal *v);
int jis_num(const JVal *v);
int jis_null(const JVal *v);
int jis_bool(const JVal *v);
const JVal *jget(const JVal *obj, const char *key);
size_t jlen(const JVal *arr);
const JVal *jat(const JVal *arr, size_t i);
const char *jstr(const JVal *s);
int jbool(const JVal *v);
int jnum_is_int(const JVal *v);
long long jint(const JVal *v);
double jnum(const JVal *v);
size_t jobj_len(const JVal *obj);
const char *jobj_key(const JVal *obj, size_t i);
const JVal *jobj_val(const JVal *obj, size_t i);

typedef struct JW JW;
JW *jw_new(void);
void jw_free(JW *jw);
char *jw_take(JW *jw, size_t *len);
void jw_raw(JW *jw, const char *s);
void jw_str(JW *jw, const char *s);
void jw_int(JW *jw, long long v);
void jw_double(JW *jw, double v);
void jw_bool(JW *jw, int v);
void jw_null(JW *jw);
void jw_obj_begin(JW *jw);
void jw_obj_end(JW *jw);
void jw_arr_begin(JW *jw);
void jw_arr_end(JW *jw);
void jw_key(JW *jw, const char *key);

#endif /* WBC_JSON_H */

static int checks = 0;
static int failures = 0;

#define CHECK(cond) do { \
    checks++; \
    if (!(cond)) { \
        fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        failures++; \
    } \
    assert(cond); \
} while (0)

/* Parse NUL-terminated text, expecting success. */
static JVal *parse_ok(const char *text)
{
    JVal *v = jparse(text, strlen(text));
    if (v == NULL)
        fprintf(stderr, "unexpected parse failure for <%s>: %s\n",
                text, jparse_err());
    return v;
}

/* Parse text expecting failure; also checks that jparse_err() is set. */
static void parse_bad(const char *text)
{
    JVal *v = jparse(text, strlen(text));
    CHECK(v == NULL);
    CHECK(jparse_err() != NULL && jparse_err()[0] != '\0');
    if (v != NULL) jfree(v);
}

static void test_scalars(void)
{
    JVal *v;

    v = parse_ok("123");
    CHECK(v != NULL);
    if (v != NULL) {
        CHECK(jis_num(v));
        CHECK(jnum_is_int(v));
        CHECK(jint(v) == 123);
        CHECK(jnum(v) == 123.0);
    }
    jfree(v);

    v = parse_ok("  true  ");
    CHECK(v != NULL && jis_bool(v) && jbool(v) == 1);
    jfree(v);

    v = parse_ok("false");
    CHECK(v != NULL && jis_bool(v) && jbool(v) == 0);
    jfree(v);

    v = parse_ok("null");
    CHECK(v != NULL && jis_null(v));
    jfree(v);

    v = parse_ok("\"hi\"");
    CHECK(v != NULL && jis_str(v) && strcmp(jstr(v), "hi") == 0);
    jfree(v);

    /* accessors are NULL-safe */
    CHECK(jget(NULL, "x") == NULL);
    CHECK(jstr(NULL) == NULL);
    CHECK(jlen(NULL) == 0);
    CHECK(jat(NULL, 0) == NULL);
    CHECK(jnum(NULL) == 0.0);
    CHECK(jint(NULL) == 0);
    CHECK(jbool(NULL) == 0);
    CHECK(jnum_is_int(NULL) == 0);
    CHECK(jis_obj(NULL) == 0 && jis_arr(NULL) == 0 && jis_str(NULL) == 0);
    CHECK(jis_num(NULL) == 0 && jis_null(NULL) == 0 && jis_bool(NULL) == 0);
    CHECK(jobj_len(NULL) == 0 && jobj_key(NULL, 0) == NULL &&
          jobj_val(NULL, 0) == NULL);
}

static void test_numbers(void)
{
    JVal *v;

    v = parse_ok("-7");
    CHECK(v != NULL && jnum_is_int(v) && jint(v) == -7);
    jfree(v);

    v = parse_ok("-0");
    CHECK(v != NULL && jnum_is_int(v) && jint(v) == 0);
    jfree(v);

    v = parse_ok("1.5");
    CHECK(v != NULL && !jnum_is_int(v) && jnum(v) == 1.5);
    jfree(v);

    v = parse_ok("0.25");
    CHECK(v != NULL && !jnum_is_int(v) && jnum(v) == 0.25);
    jfree(v);

    v = parse_ok("1e2");
    CHECK(v != NULL && !jnum_is_int(v) && jnum(v) == 100.0);
    jfree(v);

    v = parse_ok("1E+2");
    CHECK(v != NULL && jnum(v) == 100.0);
    jfree(v);

    v = parse_ok("2.5e-3");
    CHECK(v != NULL && jnum(v) == 2.5e-3);
    jfree(v);

    v = parse_ok("-1.25e+3");
    CHECK(v != NULL && jnum(v) == -1250.0);
    jfree(v);

    /* int64 boundaries stay integers */
    v = parse_ok("9223372036854775807");
    CHECK(v != NULL && jnum_is_int(v) && jint(v) == LLONG_MAX);
    jfree(v);

    v = parse_ok("-9223372036854775808");
    CHECK(v != NULL && jnum_is_int(v) && jint(v) == LLONG_MIN);
    jfree(v);

    /* one past int64 falls back to double */
    v = parse_ok("9223372036854775808");
    CHECK(v != NULL && !jnum_is_int(v) && jnum(v) == 9223372036854775808.0);
    jfree(v);

    v = parse_ok("99999999999999999999999999");
    CHECK(v != NULL && !jnum_is_int(v) && jnum(v) > 1e25);
    jfree(v);

    v = parse_ok("1e999");
    CHECK(v != NULL && jnum(v) > 1e300);   /* overflow, still a number */
    jfree(v);
}

static void test_strings(void)
{
    JVal *v;

    v = parse_ok("\"\"");
    CHECK(v != NULL && jis_str(v) && jstr(v)[0] == '\0');
    jfree(v);

    v = parse_ok("\"a\\nb\\t\\\"q\\\"\\\\\"");
    CHECK(v != NULL && strcmp(jstr(v), "a\nb\t\"q\"\\") == 0);
    jfree(v);

    v = parse_ok("\"\\/\\b\\f\\r\"");
    CHECK(v != NULL && strcmp(jstr(v), "/\b\f\r") == 0);
    jfree(v);

    /* \u escapes: ASCII, Latin-1, BMP */
    v = parse_ok("\"\\u0041\\u00e9\\u4e2d\"");
    CHECK(v != NULL && strcmp(jstr(v), "A\xc3\xa9\xe4\xb8\xad") == 0);
    jfree(v);

    /* surrogate pair -> one 4-byte UTF-8 sequence */
    v = parse_ok("\"\\ud83d\\ude00\"");
    CHECK(v != NULL && strcmp(jstr(v), "\xf0\x9f\x98\x80") == 0);
    jfree(v);

    /* lone high surrogate -> its 3-byte WTF-8 form (Python keeps the cp) */
    v = parse_ok("\"\\ud800\"");
    CHECK(v != NULL && strcmp(jstr(v), "\xed\xa0\x80") == 0);
    jfree(v);

    /* lone low surrogate -> its 3-byte WTF-8 form */
    v = parse_ok("\"\\udc00\"");
    CHECK(v != NULL && strcmp(jstr(v), "\xed\xb0\x80") == 0);
    jfree(v);

    /* high surrogate followed by a non-low escape: both survive */
    v = parse_ok("\"\\ud800\\u0041\"");
    CHECK(v != NULL && strcmp(jstr(v), "\xed\xa0\x80" "A") == 0);
    jfree(v);

    /* raw UTF-8 passes through */
    v = parse_ok("\"\xe4\xb8\xad\xe6\x96\x87\"");
    CHECK(v != NULL && strcmp(jstr(v), "\xe4\xb8\xad\xe6\x96\x87") == 0);
    jfree(v);
}

static void test_structures(void)
{
    JVal *v;
    const JVal *o;
    const JVal *b;

    v = parse_ok("{}");
    CHECK(v != NULL && jis_obj(v) && jobj_len(v) == 0);
    CHECK(v != NULL && jget(v, "missing") == NULL);
    jfree(v);

    v = parse_ok("[]");
    CHECK(v != NULL && jis_arr(v) && jlen(v) == 0);
    CHECK(v != NULL && jat(v, 0) == NULL);
    jfree(v);

    /* key order is document order */
    v = parse_ok("{\"z\":1,\"a\":2,\"m\":3}");
    CHECK(v != NULL && jobj_len(v) == 3);
    if (v != NULL) {
        CHECK(strcmp(jobj_key(v, 0), "z") == 0);
        CHECK(strcmp(jobj_key(v, 1), "a") == 0);
        CHECK(strcmp(jobj_key(v, 2), "m") == 0);
        CHECK(jint(jobj_val(v, 1)) == 2);
    }
    jfree(v);

    /* duplicate key: last value wins, first position kept (Python dict) */
    v = parse_ok("{\"a\":1,\"b\":2,\"a\":3}");
    CHECK(v != NULL && jobj_len(v) == 2);
    if (v != NULL) {
        CHECK(strcmp(jobj_key(v, 0), "a") == 0);
        CHECK(strcmp(jobj_key(v, 1), "b") == 0);
        CHECK(jint(jget(v, "a")) == 3);
    }
    jfree(v);

    /* nesting plus whitespace tolerance */
    v = parse_ok(" { \"a\" : { \"b\" : [ 1 , 2 , { \"c\" : true } ] } } \r\n\t ");
    CHECK(v != NULL && jis_obj(v));
    if (v != NULL) {
        o = jget(v, "a");
        CHECK(o != NULL && jis_obj(o));
        o = (o != NULL) ? jget(o, "b") : NULL;
        CHECK(o != NULL && jis_arr(o) && jlen(o) == 3);
        if (o != NULL) {
            CHECK(jint(jat(o, 0)) == 1);
            CHECK(jint(jat(o, 1)) == 2);
            b = jat(o, 2);
            CHECK(b != NULL && jis_obj(b));
            CHECK(b != NULL && jbool(jget(b, "c")) == 1);
        }
    }
    jfree(v);

    /* jget on a non-object is NULL */
    v = parse_ok("[1]");
    CHECK(v != NULL && jget(v, "a") == NULL);
    jfree(v);
}

/* A realistic usage-log row, the field shapes wbc_row.c reads. */
static void test_row_shape(void)
{
    const char *text =
        "{\"at\": 1728652800.125, \"model\": \"gpt-5.6-terra\", "
        "\"account\": \"u_1\", \"prompt_tokens\": 1200, "
        "\"completion_tokens\": 345, \"reasoning_tokens\": 0, "
        "\"cached_tokens\": 64, \"total_tokens\": 1609, "
        "\"credit\": 0.0021, \"outcome\": \"completed\", "
        "\"key\": \"k1\", \"realm\": \"intl\"}";
    JVal *v = parse_ok(text);

    CHECK(v != NULL && jis_obj(v));
    if (v != NULL) {
        CHECK(jnum(jget(v, "at")) == 1728652800.125);
        CHECK(strcmp(jstr(jget(v, "model")), "gpt-5.6-terra") == 0);
        CHECK(jnum_is_int(jget(v, "prompt_tokens")));
        CHECK(jint(jget(v, "prompt_tokens")) == 1200);
        CHECK(jnum_is_int(jget(v, "reasoning_tokens")));
        CHECK(jint(jget(v, "reasoning_tokens")) == 0);
        CHECK(!jnum_is_int(jget(v, "credit")));
        CHECK(jnum(jget(v, "credit")) == 0.0021);
        CHECK(jget(v, "key") != NULL);
        CHECK(strcmp(jstr(jget(v, "outcome")), "completed") == 0);
        CHECK(jget(v, "missing_field") == NULL);
    }
    jfree(v);
}

static void test_reject(void)
{
    /* empty / whitespace only */
    parse_bad("");
    parse_bad("   \n\t ");

    /* trailing garbage (Python json.loads "Extra data") */
    parse_bad("{}x");
    parse_bad("{} {}");
    parse_bad("1 2");
    parse_bad("null,null");

    /* broken literals */
    parse_bad("tru");
    parse_bad("nulll");
    parse_bad("True");

    /* NaN / Infinity are not accepted */
    parse_bad("NaN");
    parse_bad("nan");
    parse_bad("Infinity");
    parse_bad("-Infinity");
    parse_bad("[NaN]");
    parse_bad("{\"x\": Infinity}");

    /* number syntax */
    parse_bad("01");
    parse_bad("-01");
    parse_bad(".5");
    parse_bad("1.");
    parse_bad("1e");
    parse_bad("1e+");
    parse_bad("+1");
    parse_bad("-");
    parse_bad("--1");
    parse_bad("1..2");
    parse_bad("1.2.3");
    parse_bad("0x10");

    /* structure syntax */
    parse_bad("[1,]");
    parse_bad("{\"a\":1,}");
    parse_bad("[,1]");
    parse_bad("[1 2]");
    parse_bad("{\"a\" 1}");
    parse_bad("{a:1}");
    parse_bad("{\"a\":}");
    parse_bad("{\"a\"}");
    parse_bad("{\"a\":1 \"b\":2}");
    parse_bad("[");
    parse_bad("{\"a\":[1}");
    parse_bad("'a'");

    /* strings */
    parse_bad("\"abc");
    parse_bad("\"a\\x\"");
    parse_bad("\"\\u12g4\"");
    parse_bad("\"\\u12\"");
    parse_bad("\"a\nb\"");
    parse_bad("\"a\tb\"");
}

static void test_depth(void)
{
    char deep[200];
    char objdeep[512];
    int i;
    size_t n;
    JVal *v;

    /* 64 levels: accepted */
    for (i = 0; i < 64; i++) deep[i] = '[';
    deep[64] = '0';
    for (i = 0; i < 64; i++) deep[65 + i] = ']';
    deep[129] = '\0';
    v = jparse(deep, (size_t)129);
    CHECK(v != NULL);
    jfree(v);

    /* 65 levels: rejected */
    for (i = 0; i < 65; i++) deep[i] = '[';
    deep[65] = '0';
    for (i = 0; i < 65; i++) deep[66 + i] = ']';
    deep[131] = '\0';
    v = jparse(deep, (size_t)131);
    CHECK(v == NULL);
    jfree(v);

    /* 65 nested objects: rejected */
    n = 0;
    for (i = 0; i < 65; i++) { memcpy(objdeep + n, "{\"a\":", 5); n += 5; }
    objdeep[n++] = '1';
    for (i = 0; i < 65; i++) objdeep[n++] = '}';
    objdeep[n] = '\0';
    v = jparse(objdeep, n);
    CHECK(v == NULL);
    jfree(v);

    /* 64 nested objects: accepted */
    n = 0;
    for (i = 0; i < 64; i++) { memcpy(objdeep + n, "{\"a\":", 5); n += 5; }
    objdeep[n++] = '1';
    for (i = 0; i < 64; i++) objdeep[n++] = '}';
    objdeep[n] = '\0';
    v = jparse(objdeep, n);
    CHECK(v != NULL);
    jfree(v);
}

static void test_len_based(void)
{
    JVal *v;
    char *raw;

    /* len bounds the parse: "123" with len 2 is 12 */
    v = jparse("123", 2);
    CHECK(v != NULL && jnum_is_int(v) && jint(v) == 12);
    jfree(v);

    /* no NUL terminator required after len */
    raw = (char *)malloc(7);
    CHECK(raw != NULL);
    if (raw != NULL) {
        memcpy(raw, "{\"a\":1}", 7);
        v = jparse(raw, 7);
        CHECK(v != NULL && jint(jget(v, "a")) == 1);
        jfree(v);
        free(raw);
    }
}

static void test_writer(void)
{
    JW *jw;
    char *text;
    size_t len;
    JVal *v;
    const char *zh = "\xe4\xb8\xad\xe6\x96\x87";

    /* commas come from the writer; keys stay in insertion order */
    jw = jw_new();
    jw_obj_begin(jw);
    jw_key(jw, "a"); jw_int(jw, 1);
    jw_key(jw, "b"); jw_arr_begin(jw);
    jw_bool(jw, 1); jw_null(jw); jw_double(jw, 2.5);
    jw_arr_end(jw);
    jw_key(jw, "c"); jw_str(jw, "x\ny");
    jw_key(jw, "d"); jw_obj_begin(jw); jw_obj_end(jw);
    jw_key(jw, "e"); jw_arr_begin(jw); jw_arr_end(jw);
    jw_obj_end(jw);
    text = jw_take(jw, &len);
    jw_free(jw);                       /* take already detached the buffer */
    CHECK(text != NULL);
    CHECK(text != NULL && len == strlen(text));
    CHECK(text != NULL && strcmp(text,
        "{\"a\":1,\"b\":[true,null,2.5],\"c\":\"x\\ny\",\"d\":{},\"e\":[]}") == 0);

    /* the produced text parses back to the same values */
    v = (text != NULL) ? jparse(text, len) : NULL;
    CHECK(v != NULL && jis_obj(v));
    if (v != NULL) {
        CHECK(jint(jget(v, "a")) == 1);
        CHECK(jlen(jget(v, "b")) == 3);
        CHECK(jbool(jat(jget(v, "b"), 0)) == 1);
        CHECK(jis_null(jat(jget(v, "b"), 1)));
        CHECK(jnum(jat(jget(v, "b"), 2)) == 2.5);
        CHECK(strcmp(jstr(jget(v, "c")), "x\ny") == 0);
        CHECK(jobj_len(jget(v, "d")) == 0);
        CHECK(jlen(jget(v, "e")) == 0);
        CHECK(jobj_len(v) == 5);
        CHECK(strcmp(jobj_key(v, 0), "a") == 0);
        CHECK(strcmp(jobj_key(v, 4), "e") == 0);
    }
    jfree(v);
    free(text);

    /* escaping: quote, backslash, control characters */
    jw = jw_new();
    jw_str(jw, "q\"b\\c\nd\te\x01" "f");
    text = jw_take(jw, &len);
    jw_free(jw);
    CHECK(text != NULL &&
          strcmp(text, "\"q\\\"b\\\\c\\nd\\te\\u0001f\"") == 0);
    v = (text != NULL) ? jparse(text, len) : NULL;
    CHECK(v != NULL && strcmp(jstr(v), "q\"b\\c\nd\te\x01" "f") == 0);
    jfree(v);
    free(text);

    /* non-ASCII passes through as UTF-8 */
    jw = jw_new();
    jw_str(jw, zh);
    text = jw_take(jw, &len);
    jw_free(jw);
    CHECK(text != NULL && strcmp(text, "\"\xe4\xb8\xad\xe6\x96\x87\"") == 0);
    free(text);

    /* jw_str(NULL) writes "" instead of crashing */
    jw = jw_new();
    jw_str(jw, NULL);
    text = jw_take(jw, &len);
    jw_free(jw);
    CHECK(text != NULL && strcmp(text, "\"\"") == 0);
    free(text);

    /* %.17g round-trips doubles exactly */
    {
        double vals[3];
        int i;
        vals[0] = 0.1;
        vals[1] = 1.0 / 3.0;
        vals[2] = 3.141592653589793;
        for (i = 0; i < 3; i++) {
            jw = jw_new();
            jw_double(jw, vals[i]);
            text = jw_take(jw, &len);
            jw_free(jw);
            v = (text != NULL) ? jparse(text, len) : NULL;
            CHECK(v != NULL && !jnum_is_int(v) && jnum(v) == vals[i]);
            jfree(v);
            free(text);
        }
    }

    /* big integers round-trip as integers */
    jw = jw_new();
    jw_int(jw, LLONG_MAX);
    text = jw_take(jw, &len);
    jw_free(jw);
    v = (text != NULL) ? jparse(text, len) : NULL;
    CHECK(v != NULL && jnum_is_int(v) && jint(v) == LLONG_MAX);
    jfree(v);
    free(text);

    jw = jw_new();
    jw_int(jw, LLONG_MIN);
    text = jw_take(jw, &len);
    jw_free(jw);
    v = (text != NULL) ? jparse(text, len) : NULL;
    CHECK(v != NULL && jnum_is_int(v) && jint(v) == LLONG_MIN);
    jfree(v);
    free(text);

    /* jw_raw participates as a value (pre-formatted fragments) */
    jw = jw_new();
    jw_obj_begin(jw);
    jw_key(jw, "a"); jw_raw(jw, "123");
    jw_key(jw, "b"); jw_raw(jw, "[4,5]");
    jw_obj_end(jw);
    text = jw_take(jw, &len);
    jw_free(jw);
    CHECK(text != NULL && strcmp(text, "{\"a\":123,\"b\":[4,5]}") == 0);
    free(text);

    /* an untouched JW yields an empty (non-NULL) buffer */
    jw = jw_new();
    text = jw_take(jw, &len);
    jw_free(jw);
    CHECK(text != NULL && len == 0);
    free(text);

    /* parse -> write -> parse equivalence on a nested document */
    {
        const char *src =
            "{\"m\":{\"a\":[1,2.5,\"s\"],\"b\":{\"c\":false}},\"n\":null}";
        const JVal *m, *a, *b2;
        JVal *v2;

        v = parse_ok(src);
        CHECK(v != NULL);
        if (v != NULL) {
            m = jget(v, "m");
            a = (m != NULL) ? jget(m, "a") : NULL;
            b2 = (m != NULL) ? jget(m, "b") : NULL;
            CHECK(a != NULL && jlen(a) == 3);
            CHECK(b2 != NULL && jis_bool(jget(b2, "c")) && jbool(jget(b2, "c")) == 0);
            CHECK(jis_null(jget(v, "n")));

            jw = jw_new();
            jw_obj_begin(jw);
            jw_key(jw, "m");
            jw_obj_begin(jw);
            jw_key(jw, "a");
            jw_arr_begin(jw);
            jw_int(jw, jint(jat(a, 0)));
            jw_double(jw, jnum(jat(a, 1)));
            jw_str(jw, jstr(jat(a, 2)));
            jw_arr_end(jw);
            jw_key(jw, "b");
            jw_obj_begin(jw);
            jw_key(jw, "c");
            jw_bool(jw, jbool(jget(b2, "c")));
            jw_obj_end(jw);
            jw_obj_end(jw);
            jw_key(jw, "n");
            jw_null(jw);
            jw_obj_end(jw);
            text = jw_take(jw, &len);
            jw_free(jw);
            CHECK(text != NULL && strcmp(text,
                "{\"m\":{\"a\":[1,2.5,\"s\"],\"b\":{\"c\":false}},\"n\":null}") == 0);

            v2 = (text != NULL) ? jparse(text, len) : NULL;
            CHECK(v2 != NULL);
            if (v2 != NULL) {
                CHECK(jnum(jat(jget(jget(v2, "m"), "a"), 1)) == 2.5);
                CHECK(strcmp(jstr(jat(jget(jget(v2, "m"), "a"), 2)), "s") == 0);
                CHECK(jis_null(jget(v2, "n")));
            }
            jfree(v2);
            free(text);
        }
        jfree(v);
    }
}

int main(void)
{
    /* unbuffered so a crash points at the group it happened in */
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
    fprintf(stderr, "group: scalars\n");
    test_scalars();
    fprintf(stderr, "group: numbers\n");
    test_numbers();
    fprintf(stderr, "group: strings\n");
    test_strings();
    fprintf(stderr, "group: structures\n");
    test_structures();
    fprintf(stderr, "group: row_shape\n");
    test_row_shape();
    fprintf(stderr, "group: reject\n");
    test_reject();
    fprintf(stderr, "group: depth\n");
    test_depth();
    fprintf(stderr, "group: len_based\n");
    test_len_based();
    fprintf(stderr, "group: writer\n");
    test_writer();

    if (failures != 0) {
        fprintf(stderr, "JSON SELFTEST FAILED %d of %d checks\n",
                failures, checks);
        return 1;
    }
    printf("JSON SELFTEST OK %d\n", checks);
    return 0;
}
