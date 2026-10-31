//! 并发闸门（Python 的 `_chat_slots`）：同时最多 N 条对话在飞，排队等待超时就回 503。
//!
//! Python 版语义（wb_proxy.py:394-421、22659-22672）：
//! - 上限取 `WB_MAX_CONCURRENT_CHAT`，默认 32；写成 `auto|pool|dynamic` 时按就绪账号数
//!   算、下限 32
//! - 排队时间取 `WB_CHAT_SLOT_WAIT`，默认 30 秒
//! - 等不到就 503「gateway is at its concurrent chat limit (N in flight); retry shortly」，
//!   `/v1/messages` 走 Anthropic 信封（overloaded_error），其余走 OpenAI 信封
//!
//! 这里只做「拿到许可就干活、drop 就还」；释放交给 RAII，早退、客户端断连都不会漏。

use std::sync::Arc;
use std::time::Duration;

use tokio::sync::{OwnedSemaphorePermit, Semaphore};

/// Python 的 CHAT_SLOTS_FLOOR：`auto` 时再小的池也只给 32 条。
pub const FLOOR: usize = 32;

pub struct Slots {
    sem: Arc<Semaphore>,
    cap: usize,
    wait: Duration,
}

impl Slots {
    pub fn from_env(ready_accounts: usize) -> Slots {
        let raw = std::env::var("WB_MAX_CONCURRENT_CHAT")
            .unwrap_or_default()
            .trim()
            .to_lowercase();
        let cap = if matches!(raw.as_str(), "auto" | "pool" | "dynamic") {
            ready_accounts.max(FLOOR)
        } else if raw.is_empty() {
            FLOOR
        } else {
            // 解析不了就当默认值：一个笔误不该让网关起不来。
            raw.parse::<usize>().unwrap_or(FLOOR).max(1)
        };
        let wait = std::env::var("WB_CHAT_SLOT_WAIT")
            .ok()
            .and_then(|v| v.trim().parse::<f64>().ok())
            .map(|v| Duration::from_secs_f64(v.max(0.0)))
            .unwrap_or(Duration::from_secs(30));
        Slots { sem: Arc::new(Semaphore::new(cap)), cap, wait }
    }

    #[cfg(test)]
    fn with(cap: usize, wait: Duration) -> Slots {
        Slots { sem: Arc::new(Semaphore::new(cap)), cap, wait }
    }

    pub fn cap(&self) -> usize {
        self.cap
    }

    /// 等到许可就返回（drop 即释放）；超时返回 `None`，由调用方回 503。
    pub async fn acquire(&self) -> Option<OwnedSemaphorePermit> {
        match tokio::time::timeout(self.wait, self.sem.clone().acquire_owned()).await {
            Ok(Ok(permit)) => Some(permit),
            _ => None,
        }
    }

    /// 与 Python 的报错同一句话（客户端和日志都靠它认门）。
    pub fn busy_message(&self) -> String {
        format!(
            "gateway is at its concurrent chat limit ({} in flight); retry shortly",
            self.cap
        )
    }

    /// 上一条 503 的 `gateway_hint`（wb_proxy.py:13725）。
    pub const BUSY_HINT: &'static str = "gateway is busy at its concurrency limit; retry shortly";
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn auto_sizes_from_the_pool_but_never_below_the_floor() {
        std::env::remove_var("WB_MAX_CONCURRENT_CHAT");
        assert_eq!(Slots::from_env(200).cap(), FLOOR);
        std::env::set_var("WB_MAX_CONCURRENT_CHAT", "auto");
        assert_eq!(Slots::from_env(200).cap(), 200);
        assert_eq!(Slots::from_env(3).cap(), FLOOR);
        std::env::set_var("WB_MAX_CONCURRENT_CHAT", "8");
        assert_eq!(Slots::from_env(200).cap(), 8);
        std::env::set_var("WB_MAX_CONCURRENT_CHAT", "nonsense");
        assert_eq!(Slots::from_env(200).cap(), FLOOR);
        std::env::remove_var("WB_MAX_CONCURRENT_CHAT");
    }

    #[tokio::test]
    async fn the_second_request_waits_then_times_out() {
        let slots = Slots::with(1, Duration::from_millis(30));
        let held = slots.acquire().await.expect("first request gets the only slot");
        assert!(slots.acquire().await.is_none(), "满了就要排队到超时");
        drop(held);
        assert!(slots.acquire().await.is_some(), "释放之后又能进来");
    }

    #[tokio::test]
    async fn waiting_requests_get_the_slot_when_it_frees_up() {
        let slots = Arc::new(Slots::with(1, Duration::from_secs(5)));
        let held = slots.acquire().await.unwrap();
        let waiter = {
            let slots = slots.clone();
            tokio::spawn(async move { slots.acquire().await.is_some() })
        };
        tokio::time::sleep(Duration::from_millis(50)).await;
        drop(held);
        assert!(waiter.await.unwrap(), "排队的人应当在有人释放后进来");
    }

    #[test]
    fn the_busy_message_names_the_cap() {
        let slots = Slots::with(32, Duration::from_secs(1));
        assert_eq!(
            slots.busy_message(),
            "gateway is at its concurrent chat limit (32 in flight); retry shortly"
        );
    }
}
