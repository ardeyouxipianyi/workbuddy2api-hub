/* tests_pricing.c — self-test for the standalone pricing engine.
 *
 * Built alongside wbc_pricing.c + wbc_json.c + wbc_util.c (see
 * run_standalone.py).  Writes three throwaway JSONL/JSON files in the working
 * directory, exercises the ported wb_pricing.py semantics, removes the files
 * and prints "PRICING SELFTEST OK n" (n = checks run) on success; a failed
 * check prints FAIL and the process exits 1.
 *
 * Covered:
 *  - policy hit by pid (USD conversion, source/backfilled/disabled fields);
 *  - cached = min(cached, prompt);
 *  - a missing input_cache_hit bills at the miss rate;
 *  - non-USD currency gets no conversion;
 *  - unit defaults to 1e6, an explicit unit is honoured;
 *  - bands by min_prompt_tokens (last match wins, none -> flat);
 *  - bands by UTC window (days, start inclusive / end exclusive, end 2400);
 *  - a null min_prompt_tokens still takes the window path;
 *  - usd_cny defaults: policy 7.1, explicit 0 collapses to 1.0;
 *  - timeline: row at/before the stamp, exact boundary, before-first clamp,
 *    after-last row, forward backfill (price learned later, backfilled=1),
 *    backward backfill (model dropped later, backfilled=1);
 *  - unknown model -> known=0 (cny 0, source "builtin");
 *  - a dangling pid goes to the doc snapshot, never to the timeline;
 *  - doc snapshot: meta.usd_cny, currency gate, entry without currency;
 *  - a policy reference beats the doc snapshot;
 *  - no doc file -> an unpriced model stays known=0 (policies still price);
 *  - blank lines, unparsable lines and non-object lines are skipped.
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Same interface block as wbc_pricing.c (the wbc_pricing.h text, inlined
 * under the guard the header would use). */
#ifndef WBC_PRICING_H
#define WBC_PRICING_H

typedef struct Pricing Pricing;

Pricing *pricing_load(const char *policies_path, const char *timeline_path,
                      const char *doc_path);
void pricing_free(Pricing *);

typedef struct {
    int known;
    int disabled;
    double cny;
    const char *source;
    int backfilled;
} Cost;

void pricing_cost(const Pricing *p, const char *model, double at,
                  double prompt, double cached, double completion,
                  const char *pid, Cost *out);

#endif /* WBC_PRICING_H */

static int g_checks;
static int g_failed;

static void check(int cond, const char *what)
{
    g_checks++;
    if (cond) return;
    g_failed++;
    fprintf(stderr, "FAIL: %s\n", what);
}

static void check_near(double got, double want, const char *what)
{
    double tol = fabs(want) * 1e-9 + 1e-12;
    g_checks++;
    if (fabs(got - want) <= tol) return;
    g_failed++;
    fprintf(stderr, "FAIL: %s (got %.17g want %.17g)\n", what, got, want);
}

static int source_is(const Cost *c, const char *want)
{
    return c->source != NULL && strcmp(c->source, want) == 0;
}

static void write_file(const char *path, const char *text)
{
    FILE *fh = fopen(path, "wb");
    if (!fh) {
        fprintf(stderr, "cannot write %s\n", path);
        exit(2);
    }
    fputs(text, fh);
    fclose(fh);
}

/* Run pricing_cost with every field pre-set, so a field the engine forgets
 * cannot pass by accident. */
static Cost cost_of(const Pricing *p, const char *model, double at,
                    double prompt, double cached, double completion,
                    const char *pid)
{
    Cost c;
    c.known = -1;
    c.disabled = -1;
    c.cny = -1.0;
    c.source = NULL;
    c.backfilled = -1;
    pricing_cost(p, model, at, prompt, cached, completion, pid, &c);
    return c;
}

/* 2024-01-01 (Monday) 00:00:00 UTC; the band checks below are relative. */
#define MON_0000 1704067200.0

static const char POLICIES_TEXT[] =
    "\n"
    "not json at all\n"
    "[1,2]\n"
    "{\"id\":\"p1\",\"model\":\"m-flat\",\"currency\":\"USD\",\"usd_cny\":7.1,"
    "\"unit\":1000000,\"flat\":{\"input_cache_miss\":2.0,\"input_cache_hit\":0.5,"
    "\"output\":4.0},\"or_id\":\"a/m-flat\",\"first_seen\":1.0}\n"
    "{\"id\":\"p2\",\"model\":\"m-nohit\",\"currency\":\"USD\",\"usd_cny\":7.1,"
    "\"flat\":{\"input_cache_miss\":2.0,\"output\":4.0}}\n"
    "{\"id\":\"p3\",\"model\":\"m-cny\",\"currency\":\"CNY\",\"usd_cny\":7.1,"
    "\"flat\":{\"input_cache_miss\":3.0,\"input_cache_hit\":1.0,\"output\":6.0}}\n"
    "{\"id\":\"p4\",\"model\":\"m-unit\",\"currency\":\"USD\",\"usd_cny\":1.0,"
    "\"flat\":{\"input_cache_miss\":1.0,\"input_cache_hit\":1.0,\"output\":1.0}}\n"
    "{\"id\":\"p5\",\"model\":\"m-unit2\",\"currency\":\"USD\",\"usd_cny\":1.0,"
    "\"unit\":2000000,\"flat\":{\"input_cache_miss\":1.0,\"input_cache_hit\":1.0,"
    "\"output\":1.0}}\n"
    "{\"id\":\"p6\",\"model\":\"m-band\",\"currency\":\"USD\",\"usd_cny\":1.0,"
    "\"flat\":{\"input_cache_miss\":10.0,\"input_cache_hit\":10.0,\"output\":10.0},"
    "\"bands\":[{\"min_prompt_tokens\":100,\"flat\":{\"input_cache_miss\":1.0,"
    "\"input_cache_hit\":1.0,\"output\":1.0}},"
    "{\"min_prompt_tokens\":1000,\"flat\":{\"input_cache_miss\":2.0,"
    "\"input_cache_hit\":2.0,\"output\":2.0}},"
    "{\"min_prompt_tokens\":10000,\"flat\":{\"input_cache_miss\":3.0,"
    "\"input_cache_hit\":3.0,\"output\":3.0}}]}\n"
    "{\"id\":\"p7\",\"model\":\"m-time\",\"currency\":\"USD\",\"usd_cny\":1.0,"
    "\"flat\":{\"input_cache_miss\":10.0,\"input_cache_hit\":10.0,\"output\":10.0},"
    "\"bands\":[{\"days\":[\"mon\"],\"start\":100,\"end\":200,"
    "\"flat\":{\"input_cache_miss\":1.0,\"input_cache_hit\":1.0,\"output\":1.0}},"
    "{\"days\":[\"mon\"],\"start\":200,\"end\":300,"
    "\"flat\":{\"input_cache_miss\":2.0,\"input_cache_hit\":2.0,\"output\":2.0}}]}\n"
    "{\"id\":\"p8\",\"model\":\"m-nullthr\",\"currency\":\"USD\",\"usd_cny\":1.0,"
    "\"flat\":{\"input_cache_miss\":10.0,\"input_cache_hit\":10.0,\"output\":10.0},"
    "\"bands\":[{\"min_prompt_tokens\":null,\"days\":[\"mon\"],\"start\":0,"
    "\"flat\":{\"input_cache_miss\":5.0,\"input_cache_hit\":5.0,\"output\":5.0}}]}\n"
    "{\"id\":\"p9\",\"model\":\"m-openend\",\"currency\":\"USD\",\"usd_cny\":1.0,"
    "\"flat\":{\"input_cache_miss\":10.0,\"input_cache_hit\":10.0,\"output\":10.0},"
    "\"bands\":[{\"days\":[\"mon\"],\"start\":0,"
    "\"flat\":{\"input_cache_miss\":4.0,\"input_cache_hit\":4.0,\"output\":4.0}}]}\n"
    "{\"id\":\"p10\",\"model\":\"m-defaultusd\",\"currency\":\"usd\","
    "\"flat\":{\"input_cache_miss\":1.0,\"input_cache_hit\":1.0,\"output\":1.0}}\n"
    "{\"id\":\"p11\",\"model\":\"m-zerousd\",\"currency\":\"USD\",\"usd_cny\":0,"
    "\"flat\":{\"input_cache_miss\":1.0,\"input_cache_hit\":1.0,\"output\":1.0}}\n";

static const char TIMELINE_TEXT[] =
    "{\"at\":1000.0,\"policies\":{\"m-tl\":\"p1\",\"m-gone\":\"p3\",\"m-both\":\"p1\"}}\n"
    "{\"at\":2000.0,\"policies\":{\"m-tl\":\"p2\",\"m-both\":\"p1\"}}\n"
    "{\"at\":3000.0,\"policies\":{\"m-tl\":\"p1\",\"m-late\":\"p4\"}}\n";

static const char DOC_TEXT[] =
    "{\"meta\":{\"base_currency\":\"CNY\",\"usd_cny\":8.0},\"models\":{"
    "\"m-doc\":{\"display\":\"m-doc\",\"currency\":\"USD\",\"unit\":1000000,"
    "\"flat\":{\"input_cache_miss\":2.0,\"input_cache_hit\":0.5,\"output\":4.0}},"
    "\"m-doc2\":{\"unit\":1000000,\"flat\":{\"input_cache_miss\":1.0,"
    "\"input_cache_hit\":1.0,\"output\":1.0}},"
    "\"m-both\":{\"currency\":\"USD\",\"unit\":1000000,\"flat\":{"
    "\"input_cache_miss\":99.0,\"input_cache_hit\":99.0,\"output\":99.0}}}}";

int main(void)
{
    static const char *POL = "wbct_policies.jsonl";
    static const char *TL = "wbct_timeline.jsonl";
    static const char *DOC = "wbct_doc.json";
    Pricing *pr, *nodoc;
    Cost c;

    write_file(POL, POLICIES_TEXT);
    write_file(TL, TIMELINE_TEXT);
    write_file(DOC, DOC_TEXT);

    pr = pricing_load(POL, TL, DOC);
    nodoc = pricing_load(POL, TL, NULL);
    if (!pr || !nodoc) {
        fprintf(stderr, "pricing_load returned NULL\n");
        return 2;
    }

    /* ---- policy hit: pid given, USD conversion, field wiring ---------- */
    c = cost_of(pr, "m-flat", 1234567.0, 1000, 400, 100, "p1");
    check(c.known == 1, "p1 known");
    check(source_is(&c, "p1"), "p1 source");
    check(c.backfilled == 0, "p1 not backfilled");
    check(c.disabled == 0, "p1 not disabled");
    check_near(c.cny, 0.01278, "p1 cny (600*2 + 400*0.5 + 100*4)/1e6*7.1");

    /* ---- cached = min(cached, prompt) ---------------------------------- */
    c = cost_of(pr, "m-flat", 0.0, 1000, 5000, 0, "p1");
    check_near(c.cny, 0.00355, "cached clamps to prompt");

    /* ---- cache-hit rate missing: billed at the miss rate --------------- */
    c = cost_of(pr, "m-nohit", 0.0, 1000, 400, 0, "p2");
    check_near(c.cny, 0.0142, "missing input_cache_hit uses miss");

    /* ---- non-USD currency: no conversion ------------------------------- */
    c = cost_of(pr, "m-cny", 0.0, 1000, 0, 1000, "p3");
    check_near(c.cny, 0.009, "CNY entry not converted");

    /* ---- unit: default 1e6, explicit unit honoured --------------------- */
    c = cost_of(pr, "m-unit", 0.0, 2000000, 0, 0, "p4");
    check_near(c.cny, 2.0, "unit defaults to 1e6");
    c = cost_of(pr, "m-unit2", 0.0, 2000000, 0, 0, "p5");
    check_near(c.cny, 1.0, "explicit unit 2e6 honoured");

    /* ---- bands by input length: last match wins, none -> flat ---------- */
    c = cost_of(pr, "m-band", 0.0, 500, 0, 0, "p6");
    check_near(c.cny, 0.0005, "band 100 applies");
    c = cost_of(pr, "m-band", 0.0, 5000, 0, 0, "p6");
    check_near(c.cny, 0.01, "last matching band wins (1000)");
    c = cost_of(pr, "m-band", 0.0, 50000, 0, 0, "p6");
    check_near(c.cny, 0.15, "tightest band wins (10000)");
    c = cost_of(pr, "m-band", 0.0, 50, 0, 100, "p6");
    check_near(c.cny, 0.0015, "no band applies -> entry flat");

    /* ---- bands by UTC window ------------------------------------------- */
    c = cost_of(pr, "m-time", MON_0000 + 3660, 100, 0, 0, "p7");      /* Mon 01:01 */
    check_near(c.cny, 0.0001, "01:01 lands in 0100-0200");
    c = cost_of(pr, "m-time", MON_0000 + 7200, 100, 0, 0, "p7");      /* Mon 02:00 */
    check_near(c.cny, 0.0002, "02:00: start inclusive, next band");
    c = cost_of(pr, "m-time", MON_0000 + 9060, 100, 0, 0, "p7");      /* Mon 02:31 */
    check_near(c.cny, 0.0002, "02:31 lands in 0200-0300");
    c = cost_of(pr, "m-time", MON_0000 + 86400 + 3660, 100, 0, 0, "p7"); /* Tue 01:01 */
    check_near(c.cny, 0.001, "wrong weekday -> flat");
    c = cost_of(pr, "m-time", MON_0000 + 3540, 100, 0, 0, "p7");      /* Mon 00:59 */
    check_near(c.cny, 0.001, "00:59 before every window -> flat");

    /* ---- null threshold still takes the window path; end 2400 ---------- */
    c = cost_of(pr, "m-nullthr", MON_0000, 1000, 0, 0, "p8");         /* Mon 00:00 */
    check_near(c.cny, 0.005, "null threshold: window priced");
    c = cost_of(pr, "m-nullthr", MON_0000 + 86400, 1000, 0, 0, "p8"); /* Tue 00:00 */
    check_near(c.cny, 0.01, "null threshold: days gate still applies");
    c = cost_of(pr, "m-openend", MON_0000 + 86340, 1000, 0, 0, "p9"); /* Mon 23:59 */
    check_near(c.cny, 0.004, "end defaults to 2400");
    c = cost_of(pr, "m-openend", MON_0000 + 86400 + 86340, 1000, 0, 0, "p9"); /* Tue */
    check_near(c.cny, 0.01, "open end on wrong day -> flat");

    /* ---- usd_cny defaults: policy 7.1, explicit 0 collapses to 1 ------- */
    c = cost_of(pr, "m-defaultusd", 0.0, 1000000, 0, 0, "p10");
    check_near(c.cny, 7.1, "policy usd_cny defaults to 7.1 (usd matches case-insensitively)");
    c = cost_of(pr, "m-zerousd", 0.0, 1000000, 0, 0, "p11");
    check_near(c.cny, 1.0, "usd_cny 0 collapses to 1");

    /* ---- timeline: row at/before the stamp wins ------------------------ */
    c = cost_of(pr, "m-tl", 2000.0, 0, 0, 1000, NULL);
    check(c.known == 1 && source_is(&c, "p2"), "timeline row at the stamp");
    check(c.backfilled == 0, "row at the stamp not backfilled");
    check_near(c.cny, 0.0284, "timeline row rates");
    c = cost_of(pr, "m-tl", 1000.0, 1000, 0, 0, NULL);
    check(source_is(&c, "p1"), "exact at hits its own row");
    check(c.backfilled == 0, "exact at not backfilled");
    c = cost_of(pr, "m-tl", 500.0, 1000, 0, 0, NULL);
    check(source_is(&c, "p1"), "before first row clamps to row 0");
    check(c.backfilled == 0, "clamped row is not backfilled");
    c = cost_of(pr, "m-tl", 5000.0, 1000, 0, 0, NULL);
    check(source_is(&c, "p1"), "after last row uses the last assignment");

    /* ---- backfilled=1, forward: the price was learned later ------------ */
    c = cost_of(pr, "m-late", 1500.0, 1000, 0, 0, NULL);
    check(c.known == 1 && source_is(&c, "p4"), "forward backfill pid");
    check(c.backfilled == 1, "forward backfill flag");
    check_near(c.cny, 0.001, "forward backfill rates");

    /* ---- backfilled=1, backward: the model was dropped later ----------- */
    c = cost_of(pr, "m-gone", 2500.0, 0, 0, 1000, NULL);
    check(c.known == 1 && source_is(&c, "p3"), "backward backfill pid");
    check(c.backfilled == 1, "backward backfill flag");
    check_near(c.cny, 0.006, "backward backfill rates");

    /* ---- no reference at all: unpriced --------------------------------- */
    c = cost_of(pr, "m-nowhere", 2000.0, 10, 0, 10, NULL);
    check(c.known == 0, "unknown model known=0");
    check(c.cny == 0.0, "unknown model cny=0");
    check(source_is(&c, "builtin"), "unknown model source builtin");
    check(c.backfilled == 0 && c.disabled == 0, "unknown model flags");

    /* ---- a dangling pid goes to the doc, never to the timeline --------- */
    c = cost_of(pr, "m-tl", 2000.0, 0, 0, 0, "p-missing");
    check(c.known == 0, "dangling pid -> doc path, not timeline");
    check(source_is(&c, "builtin"), "dangling pid source builtin");

    /* ---- doc snapshot --------------------------------------------------- */
    c = cost_of(pr, "m-doc", 0.0, 1000, 0, 1000, NULL);
    check(c.known == 1 && source_is(&c, "builtin"), "doc hit known");
    check_near(c.cny, 0.048, "doc meta usd_cny 8.0");
    c = cost_of(pr, "m-doc2", 0.0, 1000000, 0, 0, NULL);
    check_near(c.cny, 1.0, "doc entry without currency: factor 1");

    /* ---- a policy reference beats the doc snapshot --------------------- */
    c = cost_of(pr, "m-both", 2000.0, 1000, 0, 0, NULL);
    check(source_is(&c, "p1"), "policy beats doc snapshot");
    check(c.backfilled == 0, "policy beats doc (not backfilled)");
    check_near(c.cny, 0.0142, "policy beats doc (policy rates)");

    /* ---- no doc file: an unpriced model stays known=0 ------------------ */
    c = cost_of(nodoc, "m-doc", 0.0, 1000, 0, 1000, NULL);
    check(c.known == 0 && c.cny == 0.0, "no doc -> known=0");
    c = cost_of(nodoc, "m-flat", 0.0, 1000, 0, 0, "p1");
    check(c.known == 1, "policies still price without a doc");

    pricing_free(pr);
    pricing_free(nodoc);
    remove(POL);
    remove(TL);
    remove(DOC);

    if (g_failed) {
        fprintf(stderr, "PRICING SELFTEST FAILED %d/%d\n", g_failed, g_checks);
        return 1;
    }
    printf("PRICING SELFTEST OK %d\n", g_checks);
    return 0;
}
