//! Account failure breaker and degradation state (`wb_accounts.Account`).

use std::collections::HashMap;
use std::sync::Mutex;

const BREAKER_THRESHOLD: u32 = 3;
const BREAKER_BASE: f64 = 1_800.0;
const BREAKER_MAX: f64 = 21_600.0;
const DEGRADE_THRESHOLD: u32 = 5;
const DEGRADE_BASE: f64 = 600.0;
const DEGRADE_MAX: f64 = 7_200.0;

#[derive(Clone, Copy)]
pub enum FailureKind {
    Hard,
    Unknown,
}

#[derive(Clone, Default)]
struct Failure {
    fails: u32,
    degrade_count: u32,
    last_fail_at: f64,
    breaker_until: f64,
    degrade_until: f64,
}

#[derive(Clone, Default)]
pub struct BreakerView {
    pub active_until: f64,
    pub breaker_until: f64,
    pub degrade_until: f64,
    pub fails: u32,
    pub last_fail_at: f64,
}

pub struct Breakers {
    states: Mutex<HashMap<String, Failure>>,
    fail_window: f64,
}

impl Default for Breakers {
    fn default() -> Self {
        Self {
            states: Mutex::new(HashMap::new()),
            fail_window: std::env::var("WB_FAIL_WINDOW")
                .ok()
                .and_then(|text| text.trim().parse::<f64>().ok())
                .filter(|seconds| *seconds >= 0.0)
                .unwrap_or(600.0),
        }
    }
}

impl Breakers {
    pub fn fail_window(&self) -> f64 {
        self.fail_window
    }

    pub fn cooling(&self, uid: &str, now: f64) -> bool {
        self.view(uid, now).active_until > now
    }

    pub fn failure(&self, uid: &str, kind: FailureKind, now: f64) {
        let mut states = self
            .states
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let state = states.entry(uid.to_string()).or_default();
        if self.fail_window > 0.0
            && state.last_fail_at > 0.0
            && now - state.last_fail_at > self.fail_window
        {
            state.fails = 0;
            state.degrade_count = 0;
        }
        state.last_fail_at = now;
        state.fails = state.fails.saturating_add(1);
        if matches!(kind, FailureKind::Unknown) {
            state.degrade_count = state.degrade_count.saturating_add(1);
            if state.degrade_count >= DEGRADE_THRESHOLD {
                state.degrade_until = state.degrade_until.max(
                    now + backoff(
                        state.degrade_count,
                        DEGRADE_BASE,
                        DEGRADE_MAX,
                        DEGRADE_THRESHOLD,
                    ),
                );
            }
        }
        if state.fails >= BREAKER_THRESHOLD {
            state.breaker_until = state
                .breaker_until
                .max(now + backoff(state.fails, BREAKER_BASE, BREAKER_MAX, BREAKER_THRESHOLD));
        }
    }

    pub fn success(&self, uid: &str) {
        self.states
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .remove(uid);
    }

    pub fn view(&self, uid: &str, now: f64) -> BreakerView {
        let states = self
            .states
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let Some(state) = states.get(uid) else {
            return BreakerView::default();
        };
        let deadline = state.breaker_until.max(state.degrade_until);
        BreakerView {
            active_until: if deadline > now { deadline } else { 0.0 },
            breaker_until: state.breaker_until,
            degrade_until: state.degrade_until,
            fails: state.fails,
            last_fail_at: state.last_fail_at,
        }
    }

    #[cfg(test)]
    fn failure_at(&self, uid: &str, kind: FailureKind, now: f64) {
        self.failure(uid, kind, now);
    }
}

fn backoff(count: u32, base: f64, cap: f64, offset: u32) -> f64 {
    let step = count.saturating_sub(offset).min(20);
    (base * 2_f64.powi(step as i32)).min(cap)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hard_failures_trip_the_python_breaker_ladder() {
        let breakers = Breakers::default();
        breakers.failure_at("uid", FailureKind::Hard, 100.0);
        breakers.failure_at("uid", FailureKind::Hard, 101.0);
        assert!(!breakers.cooling("uid", 102.0));
        breakers.failure_at("uid", FailureKind::Hard, 102.0);
        assert_eq!(breakers.view("uid", 102.0).breaker_until, 1_902.0);
        breakers.failure_at("uid", FailureKind::Hard, 103.0);
        assert_eq!(breakers.view("uid", 103.0).breaker_until, 3_703.0);
    }

    #[test]
    fn unknown_failures_degrade_and_share_the_breaker_counter() {
        let breakers = Breakers::default();
        for now in 100..105 {
            breakers.failure_at("uid", FailureKind::Unknown, now as f64);
        }
        let view = breakers.view("uid", 104.0);
        assert_eq!(view.fails, 5);
        assert_eq!(view.breaker_until, 7_304.0);
        assert_eq!(view.degrade_until, 704.0);
        assert_eq!(view.active_until, 7_304.0);
    }

    #[test]
    fn failures_outside_the_window_restart_and_success_clears() {
        let breakers = Breakers::default();
        breakers.failure_at("uid", FailureKind::Hard, 100.0);
        breakers.failure_at("uid", FailureKind::Hard, 101.0);
        breakers.failure_at("uid", FailureKind::Hard, 800.0);
        assert_eq!(breakers.view("uid", 800.0).fails, 1);
        breakers.success("uid");
        assert_eq!(breakers.view("uid", 800.0).fails, 0);
        assert!(!breakers.cooling("uid", 800.0));
    }
}
