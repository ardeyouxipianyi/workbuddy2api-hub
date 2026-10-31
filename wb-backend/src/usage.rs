//! usage 行写入（M1：追加 + 单进程串行）。
//!
//! Python 版是「后台写线程 + 追加」，这里用一把锁串行化追加，一条记录一次
//! write，保证不会出现半行。
//!
//! shortcut: 跨进程（切换窗口里新旧后端短暂共存）只有 O_APPEND 的原子性兜底，
//! 没有 flock；等 M3 账号池/写者守卫一起接。

use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

use serde_json::Value;

pub struct Recorder {
    dir: PathBuf,
    lock: Mutex<()>,
}

impl Recorder {
    pub fn new(dir: impl Into<PathBuf>) -> Recorder {
        Recorder { dir: dir.into(), lock: Mutex::new(()) }
    }

    pub fn path(&self) -> PathBuf {
        self.dir.join("usage.jsonl")
    }

    /// 追加一行到 `usage/usage.jsonl`；失败只记日志，绝不影响请求本身。
    pub fn append(&self, line: &Value) {
        let _guard = self.lock.lock().unwrap_or_else(|e| e.into_inner());
        let path = self.dir.join("usage.jsonl");
        let mut text = match serde_json::to_string(line) {
            Ok(t) => t,
            Err(e) => {
                eprintln!("wb-backend: usage serialize failed: {e}");
                return;
            }
        };
        text.push('\n');
        match std::fs::OpenOptions::new().create(true).append(true).open(&path) {
            Ok(mut fh) => {
                if let Err(e) = fh.write_all(text.as_bytes()) {
                    eprintln!("wb-backend: usage write failed: {e}");
                }
            }
            Err(e) => eprintln!("wb-backend: usage open failed ({}): {e}", path.display()),
        }
    }
}
