//! Realm-filtered usage time series.

use super::*;

fn bucket_at(row: &Value, origin: f64, step: i64) -> i64 {
    let at = number(row, "at").max(origin);
    origin as i64 + (((at - origin) / step as f64).floor() as i64) * step
}

/// `/usage/timeseries`: realm-filtered token and credit buckets.
pub fn timeseries(app: &Arc<App>, req: &Request<Incoming>) -> Resp {
    let rows = read_rows(app);
    let realms = account_realms(app);
    let realm = requested_realm(req, app);
    let selected = rows_for_realm(&rows, &realm, &realms);
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
    let mut buckets: BTreeMap<i64, Value> = BTreeMap::new();
    let mut credits: Vec<Value> = Vec::new();
    for row in selected {
        let at = number(row, "at");
        if at < since || at > until {
            continue;
        }
        let slot = bucket_at(row, since, step);
        let entry = buckets.entry(slot).or_insert_with(|| {
            json!({
                "at": slot, "requests": 0, "errors": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0,
                "total_tokens": 0, "credit": 0.0,
            })
        });
        if outcome(row) == "completed" {
            entry["requests"] = json!(integer(entry, "requests") + 1);
            for key in [
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "cached_tokens",
                "total_tokens",
            ] {
                entry[key] = json!(integer(entry, key) + integer(row, key));
            }
        } else {
            entry["errors"] = json!(integer(entry, "errors") + 1);
        }
        if outcome(row) != "client_aborted" {
            entry["credit"] = json!(number(entry, "credit") + number(row, "credit"));
        }
        // 「最近计费行」：Python 只收 credit > 0 的行，倒序留 50 条（面板的悬停明细）。
        if number(row, "credit") > 0.0 {
            credits.push(json!({
                "at": at,
                "iso": row.get("iso").cloned().unwrap_or_else(|| json!("")),
                "model": row.get("model").cloned().unwrap_or_else(|| json!("")),
                "account": row.get("account").cloned().unwrap_or_else(|| json!("")),
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
            "series": buckets.into_values().collect::<Vec<_>>(),
            "credits": credits,
        }),
    )
}
