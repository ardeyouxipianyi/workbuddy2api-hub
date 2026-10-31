//! 面板的增量聚合：把日志折成「日 × 时 × 模型 × 账号 × key × realm」的小桶。
//!
//! Python 侧面板之所以 1~2 ms，是因为它读日桶/时桶而不是逐行重算；rust 之前每个请求
//! 都把两万多行重折一遍（/usage 119 ms、analytics 83 ms）。这里照同样的形状做一份：
//! 只把新追加的行折进桶里，窗口视图只加桶。
//!
//! 只有「按 range 取窗口」走桶；显式的 since/until（自定义区间）仍走逐行路径，
//! 因为桶的粒度是本地小时，任意区间会差最多一小时。

use crate::pricing_store::RowCost;
use std::collections::BTreeMap;

/// 一种结果的累计量。两个视图口径不同（/usage 的 tokens 只算 completed，analytics 把
/// 失败行的 token/credit 也照实算），所以按结果分开存，取用时再合。
#[derive(Default, Clone, Copy)]
pub struct Tokens {
    pub rows: i64,
    pub prompt: i64,
    pub completion: i64,
    pub reasoning: i64,
    pub cached: i64,
    pub total: i64,
    pub credit: f64,
    pub cost: f64,
    /// 算不出价的行数（只在汇总里按模型记一笔）。
    pub missing: i64,
}

impl Tokens {
    pub fn add(&mut self, other: &Tokens) {
        self.rows += other.rows;
        self.prompt += other.prompt;
        self.completion += other.completion;
        self.reasoning += other.reasoning;
        self.cached += other.cached;
        self.total += other.total;
        self.credit += other.credit;
        self.cost += other.cost;
        self.missing += other.missing;
    }
}

/// 一个桶。除 done/failed 两组计数外，还有 analytics 要的延迟样本和账号排序用的时刻。
#[derive(Default, Clone)]
pub struct Cell {
    /// 首次出现时它在日志里的序号：并列项按首次出现排序（Python 的稳定排序）。
    /// 只有非中断的行才推进它——Python 的 feed 见到 client_aborted 直接 return。
    pub seq: u64,
    /// 这个小时桶的起点（本地整点）。窗口过滤按它比较，窗口边界都是本地午夜；
    /// 只有中断行的桶也要有起点，否则「今日」会漏掉它们的 errors 计数。
    pub start: f64,
    /// client_aborted 的行数：不进 done/failed，只有 /usage 的 errors 数它。
    pub aborted: i64,
    pub done: Tokens,
    pub failed: Tokens,
    pub ttft_sum: f64,
    pub ttft_n: i64,
    pub speed_sum: f64,
    pub speed_n: i64,
    pub elapsed_sum: f64,
    pub elapsed_n: i64,
    pub last_at: f64,
    /// 行里**原始的** realm 计数（可能为空串）：账号条目的 cross_realm 数的是这个。
    pub raw_realms: BTreeMap<String, i64>,
    /// 本桶第一行的原始 realm（账号条目建行时记的就是它）。
    pub first_raw_realm: String,
}


impl Cell {
    pub fn add_row(&mut self, row: &crate::panel_usage::Row, cost: RowCost, seq: u64, start: f64) {
        if self.start == 0.0 {
            self.start = start;
        }
        let completed = row.outcome.as_deref() == Some("completed");
        if !completed && row.outcome.as_deref() == Some("client_aborted") {
            self.aborted += 1;
            return;
        }
        if self.seq == 0 {
            self.seq = seq;
        }
        let bucket = if completed { &mut self.done } else { &mut self.failed };
        bucket.rows += 1;
        bucket.prompt += row.prompt_tokens as i64;
        bucket.completion += row.completion_tokens as i64;
        bucket.reasoning += row.reasoning_tokens as i64;
        bucket.cached += row.cached_tokens as i64;
        bucket.total += row.total_tokens as i64;
        bucket.credit += row.credit;
        if cost.known {
            bucket.cost += cost.cny;
        } else if !cost.disabled {
            bucket.missing += 1;
        }
        // 真值判断：0 与缺字段都不进样本（Python 的 `if vals[6]:`）。
        for (value, sum, count) in [
            (row.ttft_ms, &mut self.ttft_sum, &mut self.ttft_n),
            (row.tokens_per_sec, &mut self.speed_sum, &mut self.speed_n),
            (row.elapsed_ms, &mut self.elapsed_sum, &mut self.elapsed_n),
        ] {
            if let Some(value) = value.filter(|value| *value != 0.0) {
                *sum += value;
                *count += 1;
            }
        }
        if row.at > self.last_at {
            self.last_at = row.at;
        }
        let raw = row.realm.clone().unwrap_or_default();
        // 本桶的第一行：账号条目建行时记的 realm 就是它。
        if self.raw_realms.is_empty() {
            self.first_raw_realm = raw.clone();
        }
        *self.raw_realms.entry(raw).or_default() += 1;
    }

    pub fn merge(&mut self, other: &Cell) {
        self.done.add(&other.done);
        self.failed.add(&other.failed);
        self.aborted += other.aborted;
        if self.start == 0.0 || (other.start != 0.0 && other.start < self.start) {
            self.start = other.start;
        }
        self.ttft_sum += other.ttft_sum;
        self.ttft_n += other.ttft_n;
        self.speed_sum += other.speed_sum;
        self.speed_n += other.speed_n;
        self.elapsed_sum += other.elapsed_sum;
        self.elapsed_n += other.elapsed_n;
        if other.last_at > self.last_at {
            self.last_at = other.last_at;
        }
        if other.seq != 0 && (self.seq == 0 || other.seq < self.seq) {
            self.seq = other.seq;
            self.first_raw_realm = other.first_raw_realm.clone();
        }
        for (realm, count) in &other.raw_realms {
            *self.raw_realms.entry(realm.clone()).or_default() += count;
        }
    }
}

/// 桶的维度键。`realm` 是行归属的出口（`row_realm`）；`key` 为空串表示这把 key
/// 出现在配置里之前的老行（渲染时映射成 `__before_keys__`）。
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct CellKey {
    pub model: String,
    pub account: String,
    pub key: String,
    pub realm: String,
}

#[derive(Default, Clone)]
pub struct Index {
    /// `hours[(日, 时)][维度键]`：日/时单独分层，窗口只碰相关的日。
    pub hours: BTreeMap<(i32, i32), BTreeMap<CellKey, Cell>>,
    /// 折进来的行数（判废/预热用）。
    pub rows: u64,
}

impl Index {
    /// 窗口内的桶：`since`/`until` 是本地午夜，`realm` 传 "all" 表示不过滤出口。
    pub fn window<'a>(
        &'a self,
        since: Option<f64>,
        until: Option<f64>,
        realm: &'a str,
    ) -> impl Iterator<Item = (&'a CellKey, &'a Cell)> + 'a {
        self.hours
            .values()
            .flat_map(|cells| cells.iter())
            .filter(move |(key, cell)| {
                (realm == "all" || key.realm == realm)
                    && since.map(|lower| cell.start + 3600.0 > lower).unwrap_or(true)
                    && until.map(|upper| cell.start <= upper).unwrap_or(true)
            })
    }
}

#[cfg(test)]
mod tests {
    use super::{Cell, CellKey, Index};
    use crate::panel_usage::Row;
    use crate::pricing_store::RowCost;

    fn cost() -> RowCost {
        RowCost { cny: 0.5, known: true, disabled: false }
    }

    fn row(line: &str) -> Row {
        Row::parse(line).unwrap()
    }

    fn put(index: &mut Index, line: &str, at: f64) {
        let row = row(line);
        let seq = index.rows;
        index.rows += 1;
        let key = CellKey {
            model: row.model.clone().unwrap_or_default(),
            account: row.account.clone().unwrap_or_default(),
            key: row.key.clone().unwrap_or_default(),
            realm: row.realm.clone().unwrap_or_default(),
        };
        index
            .hours
            .entry((1, 0))
            .or_default()
            .entry(key)
            .or_default()
            .add_row(&row, cost(), seq, at);
    }

    #[test]
    fn aborted_rows_only_count_as_aborted_and_do_not_advance_seq() {
        let mut index = Index::default();
        let aborted = r#"{"outcome": "client_aborted", "model": "m", "account": "a", "total_tokens": 9}"#;
        let done = r#"{"outcome": "completed", "model": "m", "account": "a", "total_tokens": 5}"#;
        put(&mut index, aborted, 100.0);
        put(&mut index, done, 100.0);
        let (_, cell) = index.window(None, None, "all").next().unwrap();
        assert_eq!(cell.aborted, 1);
        assert_eq!(cell.done.rows, 1);
        assert_eq!(cell.failed.rows, 0);
        // 序号只由非中断行推进：中断行不能把并列项的首次出现顺序往前顶。
        assert_eq!(cell.seq, 1);
        assert_eq!(cell.done.total, 5);
    }

    #[test]
    fn window_filters_by_hour_start_realm_and_bounds() {
        let mut index = Index::default();
        put(&mut index, r#"{"outcome": "completed", "model": "m", "account": "a", "realm": "cn"}"#, 1000.0);
        put(&mut index, r#"{"outcome": "completed", "model": "m", "account": "a", "realm": "intl"}"#, 1000.0);
        assert_eq!(index.window(None, None, "all").count(), 2);
        assert_eq!(index.window(None, None, "cn").count(), 1);
        // 下界是本地午夜、桶是整点：跨过下界的那一小时算进来，整个落在更早的桶不算。
        assert_eq!(index.window(Some(1000.0), None, "all").count(), 2);
        assert_eq!(index.window(Some(4600.0), None, "all").count(), 0);
        assert_eq!(index.window(None, Some(999.0), "all").count(), 0);
    }
}
