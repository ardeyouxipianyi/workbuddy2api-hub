/* probe_round.c -- boundary checks for the Python-compatible rounding. */
#include <stdio.h>
#include <string.h>

#include "wbc_util.h"

static int checks = 0, failures = 0;

static void expect(const char *label, double got, double want)
{
    checks++;
    if (got != want) {
        failures++;
        fprintf(stderr, "FAIL %s: got %.17g want %.17g\n", label, got, want);
    }
}

int main(void)
{
    /* the real-log boundary: 105937/20 = 5296.85 (a double slightly above the
     * decimal tie) must round UP like Python's round(v, 1) */
    expect("avg 5296.85 -> 5296.9", py_round1(5296.85), 5296.9);
    /* ties to even on the one-decimal grid; 0.35 is a double BELOW the tie
     * (0.34999999999999997779...) so Python rounds it down - expectations
     * below are Python's own round() output, verified on the same machine */
    expect("0.25 -> 0.2", py_round1(0.25), 0.2);
    expect("0.35 -> 0.3", py_round1(0.35), 0.3);
    expect("2.5 -> 2", py_round_half_even(2.5), 2.0);
    expect("3.5 -> 4", py_round_half_even(3.5), 4.0);
    expect("-2.5 -> -2", py_round_half_even(-2.5), -2.0);
    expect("-0.5 -> 0", py_round_half_even(-0.5), 0.0);
    expect("0.5 -> 0", py_round_half_even(0.5), 0.0);
    expect("1.5 -> 2", py_round_half_even(1.5), 2.0);
    /* ordinary values */
    expect("100.04 -> 100.0", py_round1(100.04), 100.0);
    expect("100.05 -> 100.0", py_round1(100.05), 100.0);
    expect("100.15 -> 100.2", py_round1(100.15), 100.2);
    expect("50.0 -> 50.0", py_round1(50.0), 50.0);
    expect("0.0 -> 0.0", py_round1(0.0), 0.0);
    expect("123456789.0", py_round1(123456789.0), 123456789.0);
    expect("rank 1.5 -> 2", py_round_half_even(1.5), 2.0);
    expect("rank 2.5 -> 2", py_round_half_even(2.5), 2.0);
    if (failures) {
        fprintf(stderr, "ROUND SELFTEST FAILED %d of %d\n", failures, checks);
        return 1;
    }
    printf("ROUND SELFTEST OK %d\n", checks);
    return 0;
}
