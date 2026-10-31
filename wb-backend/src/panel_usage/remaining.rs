//! Conservative remaining-quota estimates built from the usage log.

use super::*;

type PairKey = (String, String, String);
type PairValue = (i64, String);

fn collect_pairs(
    rows: &[Value],
    accounts: &[crate::state::AccountFile],
    realms: &BTreeMap<String, String>,
    floor: f64,
    current: &str,
) -> BTreeMap<PairKey, PairValue> {
    let mut pairs: BTreeMap<(String, String, String), (i64, String)> = BTreeMap::new();
    for row in rows {
        if number(row, "at") < floor {
            continue;
        }
        let uid = row
            .get("account")
            .and_then(Value::as_str)
            .unwrap_or("(unattributed)")
            .to_string();
        let realm = row_realm(row, &realms, current);
        let model = row
            .get("model")
            .and_then(Value::as_str)
            .unwrap_or("?")
            .to_string();
        let account = accounts.iter().find(|account| account.uid() == uid);
        let nickname = account
            .map(|value| value.str("nickname").to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| uid.chars().take(8).collect());
        let entry = pairs.entry((uid, realm, model)).or_insert((0, nickname));
        entry.0 += integer(row, "total_tokens");
    }
    pairs
}

fn remaining_row(uid: String, nickname: String, realm: String, model: String, used: i64) -> Value {
    json!({
        "uid": uid,
        "nickname": nickname,
        "realm": realm,
        "model": model,
        "used": used,
        "budget": null,
        "budget_min": null,
        "budget_max": null,
        "samples": 0,
        "remaining": null,
        "used_pct": null,
        "window_start": crate::state::now_epoch() as f64 - DAY_SECONDS,
        "window_iso": crate::state::local_stamp(),
        "reset_at": null,
        "reset_iso": null,
        "cooling": false,
        "estimated": true,
        "segments": [],
    })
}

fn model_totals(pairs: BTreeMap<PairKey, PairValue>, floor: f64) -> (Vec<Value>, Vec<Value>) {
    let mut models: BTreeMap<(String, String), Value> = BTreeMap::new();
    let mut output_rows = Vec::with_capacity(pairs.len());
    for ((uid, realm, model), (used, nickname)) in pairs {
        let totals = models
            .entry((realm.clone(), model.clone()))
            .or_insert_with(|| {
                json!({
                    "realm": realm.clone(),
                    "model": model.clone(),
                    "total_remaining": null,
                    "budget": null,
                    "pairs": 0,
                    "available": 0,
                    "cooling": 0,
                    "unknown": 0,
                    "estimated": 0,
                })
            });
        totals["pairs"] = json!(integer(totals, "pairs") + 1);
        totals["unknown"] = json!(integer(totals, "unknown") + 1);
        totals["estimated"] = json!(integer(totals, "estimated") + 1);
        let mut row = remaining_row(uid, nickname, realm, model, used);
        row["window_start"] = json!(floor);
        output_rows.push(row);
    }
    (models.into_values().collect(), output_rows)
}

/// 本地时区里「昨天 00:00」的 epoch——判定下界取整天边界而不是 now−24h，
/// 同一天里不变，面板轮询之间才不会抖（Python 的 _remaining_activity_floor）。
fn yesterday_midnight() -> f64 {
    local_midnight(1).unwrap_or_else(|| crate::state::now_epoch() as f64 - DAY_SECONDS)
}

fn local_stamp_with_seconds(epoch: f64) -> String {
    unsafe {
        let time = epoch as libc::time_t;
        let mut value: libc::tm = std::mem::zeroed();
        if libc::localtime_r(&time, &mut value).is_null() {
            return String::new();
        }
        format!(
            "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
            value.tm_year + 1900,
            value.tm_mon + 1,
            value.tm_mday,
            value.tm_hour,
            value.tm_min,
            value.tm_sec
        )
    }
}

pub fn remaining(app: &Arc<App>) -> Resp {
    let now = crate::state::now_epoch() as f64;
    let floor = yesterday_midnight();
    let rows = read_rows(app);
    let accounts = app.state.accounts_snapshot();
    let realms = account_realms(app);
    // shortcut: 这里按 usage 活动取 (账号, 模型) 对；Python 还要用它自己的 cap 来源
    // （limit-events.jsonl + 凭证档 model_caps）过滤，那套子系统尚未移植 —— 有 cap
    // 的部署上两边行集合会不同，属已知缺口（PANEL_TODO 里登记）。
    let current = app.state.current_realm();
    let pairs = collect_pairs(&rows, &accounts, &realms, floor, &current);
    let (models, output_rows) = model_totals(pairs, floor);
    json_resp(
        StatusCode::OK,
        &json!({
            "window_seconds": 86_400,
            "generated_at": now,
            "generated_iso": crate::state::local_stamp(),
            "activity_floor": floor,
            "activity_floor_iso": local_stamp_with_seconds(floor),
            "budgets": [],
            "models": models,
            "rows": output_rows,
        }),
    )
}
