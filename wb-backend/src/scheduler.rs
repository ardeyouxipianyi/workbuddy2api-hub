//! 后台定时任务：签到 + 成长任务（每日 09:00 本地）与积分刷新（每 6 小时）。
//!
//! shortcut: 只做了这两件事；窗口探针（模型额度窗口）、额度扫描、国际版活跃没移植，
//! 所以面板的 `windows`/`quota_scans` 是空数组，`enabled` 只存在内存里（重启回默认开）。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};

use crate::App;

pub struct Scheduler {
    enabled: AtomicBool,
    last_run: Mutex<String>,
    /// 当天已经跑过的那一班（`YYYY-MM-DD 09`），防同一小时重复执行。
    last_daily: Mutex<String>,
}

impl Default for Scheduler {
    fn default() -> Self {
        Self {
            enabled: AtomicBool::new(true),
            last_run: Mutex::new(String::new()),
            last_daily: Mutex::new(String::new()),
        }
    }
}

impl Scheduler {
    pub fn enabled(&self) -> bool {
        self.enabled.load(Ordering::Relaxed)
    }

    pub fn set_enabled(&self, on: bool) {
        self.enabled.store(on, Ordering::Relaxed);
    }

    pub fn last_run(&self) -> String {
        self.last_run.lock().map(|s| s.clone()).unwrap_or_default()
    }

    fn mark_run(&self) {
        if let Ok(mut slot) = self.last_run.lock() {
            *slot = crate::state::local_stamp();
        }
    }

    /// `GET /scheduler`：面板顶栏那个状态条。
    pub fn status(&self) -> Value {
        let now = crate::state::local_stamp();
        json!({
            "enabled": self.enabled(),
            "mode": "每日 09:00 签到与成长任务，每 6 小时刷新积分",
            "mode_cn": "每日 09:00 签到与成长任务，每 6 小时刷新积分",
            "mode_intl": "每 6 小时刷新积分（国际版活跃未移植）",
            "last_run_time": self.last_run(),
            "next_run_time": next_run_label(&now),
            "windows": [],
            "quota_scans": [],
            "daily_chat": {},
            "logs": [],
        })
    }
}

/// 下一个整点班次（只报「下一次 09:00」和「下一次 6 小时刷新」里更早的那个）。
fn next_run_label(now: &str) -> String {
    let hour: i64 = now.get(11..13).and_then(|h| h.parse().ok()).unwrap_or(0);
    let date = now.get(..10).unwrap_or("").to_string();
    if hour < 9 {
        return format!("{date} 09:00");
    }
    let next_credit = ((hour / 6) + 1) * 6;
    if next_credit < 24 {
        format!("{date} {next_credit:02}:00")
    } else {
        format!("{date} 次日 00:00")
    }
}

/// 起一个 60 秒一跳的后台循环。
pub fn spawn(app: Arc<App>) {
    tokio::spawn(async move {
        let mut last_credit: i64 = crate::state::now_epoch();
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(60)).await;
            if !app.scheduler.enabled() {
                continue;
            }
            let stamp = crate::state::local_stamp();
            let hour: i64 = stamp.get(11..13).and_then(|h| h.parse().ok()).unwrap_or(0);
            let slot = format!("{} {hour:02}", stamp.get(..10).unwrap_or(""));

            // 每日 09:00：签到 + 成长任务（只挑参与任务的国内版账号）。
            if hour == 9 {
                let fresh = app
                    .scheduler
                    .last_daily
                    .lock()
                    .map(|s| *s != slot)
                    .unwrap_or(false);
                if fresh {
                    if let Ok(mut guard) = app.scheduler.last_daily.lock() {
                        *guard = slot;
                    }
                    daily(&app).await;
                }
            }

            // 每 6 小时刷一次积分（余额/签到状态都靠它保持新鲜）。
            if crate::state::now_epoch() - last_credit >= 6 * 3600 {
                last_credit = crate::state::now_epoch();
                crate::credits::sweep(&app, "", "").await;
                app.scheduler.mark_run();
            }
        }
    });
}

/// 每日那一班：先签到，再跑成长任务。
pub async fn daily(app: &Arc<App>) -> Value {
    let checkin = crate::credits::checkin_all(app, "").await;
    let run = crate::tasks::run(app, "", true).await;
    app.scheduler.mark_run();
    json!({"ok": true, "checkin": checkin, "tasks": run})
}
