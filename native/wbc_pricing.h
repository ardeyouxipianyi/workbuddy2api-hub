/* wbc_pricing.h -- pricing engine interface (see CONTRACT.md). */
#ifndef WBC_PRICING_H
#define WBC_PRICING_H

typedef struct Pricing Pricing;

/* policies/timeline are JSONL paths; doc_path is an optional pricing.json
 * (pass NULL for "no factory snapshot", rows it would price read unknown). */
Pricing *pricing_load(const char *policies_path, const char *timeline_path,
                      const char *doc_path);
void pricing_free(Pricing *p);

typedef struct {
    int known;              /* a price was found */
    int disabled;           /* master switch off (always 0 in this engine) */
    double cny;
    const char *source;     /* the policy id, or "builtin" */
    int backfilled;         /* the price was learned after the fact */
} Cost;

/* The row's own pid wins; without one the timeline answers, and a model no
 * policy covers is priced from the factory snapshot.  `source` points into
 * the Pricing object and stays valid until pricing_free. */
void pricing_cost(const Pricing *p, const char *model, double at,
                  double prompt, double cached, double completion,
                  const char *pid, Cost *out);

#endif
