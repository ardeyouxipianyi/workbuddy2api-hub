//! Realm-filtered usage time series.

use super::*;

/// 一格累加器。原来是 BTreeMap<i64, Value>：每行对每个字段都要
/// `integer(entry, k) + integer(row, k)` 再塞回 Value，等于每行十几次装箱/取箱。
/// 都是加法，与逐行口径等价，所以先攒成结构、最后一次性序列化。
#[derive(Default)]
struct Slot {
    requests: i64,
    errors: i64,
    prompt_tokens: i64,
    completion_tokens: i64,
    reasoning_tokens: i64,
    cached_tokens: i64,
    total_tokens: i64,
    credit: f64,
}

impl Slot {
    fn json(&self, at: i64) -> Value {
        json!({
            "at": at, "requests": self.requests, "errors": self.errors,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens, "credit": self.credit,
        })
    }
}

fn bucket_at(row: &Row, origin: f64, step: i64) -> i64 {
    let at = number(row, "at").max(origin);
    origin as i64 + (((at - origin) / step as f64).floor() as i64) * step
}

/// `/usage/timeseries`: realm-filtered token and credit buckets.
pub fn timeseries(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let rows = read_rows(app);
    let realms = account_realms(app);
    let realm = requested_realm(req, app);
    let current = app.state.current_realm();
    let selected = rows_for_realm(&rows, &realm, &realms, &current);
    // 同 analytics：range=today|week|month 是面板的区间按钮，必须换算成边界。
    let (range_since, range_until) = range_bounds(req);
    let until = query(req, "until")
        .and_then(|value| value.parse::<f64>().ok())
        .or(range_until)
        .unwrap_or_else(epoch_now);
    let since = query(req, "since")
        .and_then(|value| value.parse::<f64>().ok())
        .or(range_since)
        .unwrap_or(until - DAY_SECONDS);
    let step = query(req, "bucket")
        .and_then(|value| value.parse::<i64>().ok())
        .map(|value| value.max(60))
        .unwrap_or_else(|| {
            if until - since <= 6.0 * 3600.0 {
                60
            } else if until - since <= 14.0 * DAY_SECONDS {
                3_600
            } else {
                86_400
            }
        });
    let mut buckets: BTreeMap<i64, Slot> = BTreeMap::new();
    let mut credits: Vec<Value> = Vec::new();
    for row in selected {
        let at = number(row, "at");
        if at < since || at > until {
            continue;
        }
        let slot = buckets.entry(bucket_at(row, since, step)).or_default();
        if outcome(row) == "completed" {
            slot.requests += 1;
            slot.prompt_tokens += integer(row, "prompt_tokens");
            slot.completion_tokens += integer(row, "completion_tokens");
            slot.reasoning_tokens += integer(row, "reasoning_tokens");
            slot.cached_tokens += integer(row, "cached_tokens");
            slot.total_tokens += integer(row, "total_tokens");
        } else {
            slot.errors += 1;
        }
        if outcome(row) != "client_aborted" {
            slot.credit += number(row, "credit");
        }
        // 「最近计费行」：Python 只收 credit > 0 的行，倒序留 50 条（面板的悬停明细）。
        if number(row, "credit") > 0.0 {
            credits.push(json!({
                "at": at,
                "iso": row.iso.clone().unwrap_or_default(),
                "model": row.model.clone().unwrap_or_default(),
                "account": row.account.clone().unwrap_or_default(),
                "credit": number(row, "credit"),
                "total_tokens": integer(row, "total_tokens"),
            }));
        }
    }
    credits.sort_by(|left, right| {
        number(right, "at")
            .partial_cmp(&number(left, "at"))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    credits.truncate(50);
    json_resp(
        StatusCode::OK,
        &json!({
            "ok": true,
            "realm": realm,
            "bucket_seconds": step,
            "since": since,
            "until": until,
            "series": buckets.iter().map(|(at, slot)| slot.json(*at)).collect::<Vec<_>>(),
            "credits": credits,
        }),
    )
}
