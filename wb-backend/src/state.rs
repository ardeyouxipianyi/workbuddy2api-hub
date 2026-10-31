//! 账号与设置：只读加载。
//!
//! 账号档用 `serde_json::Map` 原样保留——Python 版是 dict 读写，Rust 这边必须
//! 能原样写回（M3 起要保存 token/状态），任何「只认识的字段」struct 都会静默
//! 丢掉未知字段，那是数据损坏，不是省事。

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::RwLock;

use serde_json::{Map, Value};

/// 与 wb_accounts.py:4100 同一份清单：和凭证同目录但不是账号的文件。
const NON_ACCOUNT_FILES: &[&str] = &[
    "settings.json",
    "active_realm.json",
    "model_windows.json",
    "quota_scan.json",
];

#[derive(Clone)]
pub struct AccountFile {
    pub path: PathBuf,
    pub name: String,
    pub data: Map<String, Value>,
}

impl AccountFile {
    pub fn str(&self, key: &str) -> &str {
        self.data.get(key).and_then(Value::as_str).unwrap_or("")
    }

    pub fn uid(&self) -> String {
        self.str("uid").to_string()
    }

    /// 静态 API Key 只在国内出口有效（wb_accounts.py:899），其余看落盘字段。
    pub fn realm(&self) -> String {
        if self.is_api_key() {
            return "cn".into();
        }
        match self.str("realm") {
            "" => "intl".into(),
            other => other.to_string(),
        }
    }

    pub fn enabled(&self) -> bool {
        self.data.get("enabled").and_then(Value::as_bool).unwrap_or(true)
    }

    /// 账号自带的出站域名（`X-Domain`）；空则回落区域默认。
    pub fn domain(&self) -> String {
        self.str("domain").to_string()
    }

    /// 身分：desktop / vscode / cli，决定上游端点（wb_identity._ENDPOINTS）。
    pub fn product(&self) -> String {
        match self.str("product") {
            "" => "desktop".into(),
            other => other.to_string(),
        }
    }

    pub fn access_token(&self) -> String {
        self.str("accessToken").to_string()
    }

    pub fn refresh_token(&self) -> String {
        self.str("refreshToken").to_string()
    }

    pub fn file_name(&self) -> String {
        self.name.clone()
    }

    pub fn is_api_key(&self) -> bool {
        self.str("accessToken").starts_with("ck_")
    }

    pub fn expires_at(&self) -> i64 {
        self.data.get("expiresAt").and_then(Value::as_i64).unwrap_or(0)
    }

    /// ready() 的判定只用 M1 需要的三条：启用、有 token、没过期。
    /// 冷却/熔断/限额属于账号池（M3），不在这里。
    pub fn ready(&self, now: i64) -> bool {
        if !self.data.get("enabled").map(|v| v.as_bool().unwrap_or(true)).unwrap_or(true) {
            return false;
        }
        if self.str("accessToken").is_empty() {
            return false;
        }
        let expires = self.data.get("expiresAt").and_then(Value::as_i64).unwrap_or(0);
        // expiresAt=0 是静态 API Key 的「永不按时间淘汰」，不是「1970 过期」。
        expires == 0 || expires > now
    }
}

pub struct State {
    pub accounts_dir: PathBuf,
    pub usage_dir: PathBuf,
    accounts: RwLock<Vec<AccountFile>>,
    pub settings: RwLock<Map<String, Value>>,
    /// 启动时刻读完的账号快照，用于 M1 的「读」语义；写入留给 M3。
    pub model_catalog: RwLock<Value>,
    /// 上次读盘时这两个文件的 mtime；用来「变了才重读」，不是节流。
    stamp: std::sync::Mutex<(u64, u64)>,
}

impl State {
    pub fn load(accounts_dir: &str, usage_dir: &str) -> State {
        let accounts_dir = PathBuf::from(accounts_dir);
        let settings = read_settings(&accounts_dir);
        let state = State {
            accounts_dir: accounts_dir.clone(),
            usage_dir: PathBuf::from(usage_dir),
            accounts: RwLock::new(load_accounts(&accounts_dir)),
            settings: RwLock::new(settings),
            model_catalog: RwLock::new(read_json(accounts_dir.join("model.json"))
                .unwrap_or(Value::Null)),
            stamp: std::sync::Mutex::new((0, 0)),
        };
        state.refresh();
        state
    }

    /// 磁盘变了才重读账号与设置：面板改 key/切区域/换号都靠它立刻生效。
    pub fn refresh(&self) {
        let settings_m = mtime(&self.accounts_dir.join("settings.json"));
        let accounts_m = mtime(&self.accounts_dir);
        {
            let mut stamp = self.stamp.lock().unwrap();
            if *stamp == (settings_m, accounts_m) {
                return;
            }
            *stamp = (settings_m, accounts_m);
        }
        *self.settings.write().unwrap() = read_settings(&self.accounts_dir);
        *self.accounts.write().unwrap() = load_accounts(&self.accounts_dir);
    }

    pub fn settings(&self) -> Map<String, Value> {
        self.settings.read().unwrap().clone()
    }

    pub fn accounts_snapshot(&self) -> Vec<AccountFile> {
        self.accounts.read().map(|g| g.clone()).unwrap_or_default()
    }

    pub fn settings_snapshot(&self) -> Map<String, Value> {
        self.settings.read().map(|g| g.clone()).unwrap_or_default()
    }

    pub fn accounts_dir_name(&self) -> String {
        self.accounts_dir.display().to_string()
    }

    pub fn accounts(&self) -> std::sync::RwLockReadGuard<'_, Vec<AccountFile>> {
        self.accounts.read().unwrap()
    }

    pub fn reload(&self) {
        let fresh = load_accounts(&self.accounts_dir);
        *self.accounts.write().unwrap() = fresh;
    }

    pub fn ready_count(&self) -> usize {
        let now = now_epoch();
        self.accounts().iter().filter(|a| a.ready(now)).count()
    }

    /// 区域开关：`accounts/active_realm.json` 的 `{"realm": "cn"}`，缺省 intl
    /// （wb_proxy.py:6069 load_persisted_realm）。每次现读，面板切区域立刻生效。
    pub fn current_realm(&self) -> String {
        match read_json(self.accounts_dir.join("active_realm.json")) {
            Some(Value::Object(m)) => match m.get("realm").and_then(Value::as_str) {
                Some("cn") => "cn".into(),
                Some("intl") => "intl".into(),
                _ => "intl".into(),
            },
            _ => "intl".into(),
        }
    }

    /// 面板里配了 Key 就意味着要鉴权；LAN 免鉴权等细则随 M2 的鉴权一起接。
    pub fn api_key_required(&self) -> bool {
        self.settings
            .read()
            .unwrap()
            .get("api_keys")
            .and_then(Value::as_array)
            .map(|keys| keys.iter().any(|k| {
                k.get("key").and_then(Value::as_str).map(|s| !s.is_empty()).unwrap_or(false)
            }))
            .unwrap_or(false)
    }

    pub fn setting_str(&self, key: &str) -> String {
        self.settings
            .read()
            .unwrap()
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    }

    pub fn setting_bool(&self, key: &str, default: bool) -> bool {
        self.settings
            .read()
            .unwrap()
            .get(key)
            .and_then(Value::as_bool)
            .unwrap_or(default)
    }
}

/// `wb_fingerprint.derive_id`：md5("<salt>:<uid>") 改成 UUIDv4 形状。
pub fn derive_id(uid: &str, salt: &str) -> String {
    use md5::{Digest, Md5};
    let digest = format!("{:x}", Md5::digest(format!("{salt}:{}", if uid.is_empty() { "anonymous" } else { uid }).as_bytes()));
    let bytes: Vec<char> = digest.chars().collect();
    let mut h: Vec<char> = bytes[..32].to_vec();
    h[12] = '4';
    h[16] = 'a';
    let s: String = h.into_iter().collect();
    format!("{}-{}-{}-{}-{}", &s[0..8], &s[8..12], &s[12..16], &s[16..20], &s[20..32])
}

/// JWT 的 exp（秒）；解析不出来就是 None，调用方自己回落。
pub fn jwt_exp(token: &str) -> Option<i64> {
    let mut parts = token.split('.');
    let _header = parts.next()?;
    let payload = parts.next()?;
    let raw = base64url_decode(payload)?;
    let value: Value = serde_json::from_slice(&raw).ok()?;
    value.get("exp").and_then(Value::as_i64)
}

fn base64url_decode(input: &str) -> Option<Vec<u8>> {
    const TABLE: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = Vec::new();
    let mut acc: u32 = 0;
    let mut bits = 0;
    for ch in input.bytes() {
        if ch == b'=' {
            break;
        }
        let value = TABLE.iter().position(|c| *c == ch)? as u32;
        acc = (acc << 6) | value;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
        }
    }
    Some(out)
}

pub fn now_epoch() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn mtime(path: impl AsRef<Path>) -> u64 {
    std::fs::metadata(path.as_ref())
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn read_json(path: impl AsRef<Path>) -> Option<Value> {
    let text = std::fs::read_to_string(path.as_ref()).ok()?;
    serde_json::from_str(&text).ok()
}

fn read_settings(accounts_dir: &Path) -> Map<String, Value> {
    match read_json(accounts_dir.join("settings.json")) {
        Some(Value::Object(map)) => map,
        _ => Map::new(),
    }
}

fn load_accounts(accounts_dir: &Path) -> Vec<AccountFile> {
    let mut out = Vec::new();
    let entries = match std::fs::read_dir(accounts_dir) {
        Ok(entries) => entries,
        Err(_) => return out,
    };
    // sorted(os.listdir(...))：顺序影响 /v1/models 与选号的确定性，别换成 dir 顺序。
    let mut names: Vec<String> = entries
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| n.ends_with(".json") && !NON_ACCOUNT_FILES.contains(&n.as_str()))
        .collect();
    names.sort();
    for name in names {
        let path = accounts_dir.join(&name);
        match read_json(&path) {
            Some(Value::Object(data)) => {
                // uid 缺失时 Python 会从 token 里解；M1 不做 JWT 解析（M3 的 OAuth 一起做），
                // 用文件名兜底，保证不会把空 uid 当键用。
                let mut data = data;
                if data.get("uid").and_then(Value::as_str).unwrap_or("").is_empty() {
                    let stem = name.trim_end_matches(".json").to_string();
                    data.insert("uid".into(), Value::String(stem));
                }
                out.push(AccountFile { path, name, data });
            }
            _ => eprintln!("wb-backend: skip account file that is not a JSON object: {name}"),
        }
    }
    out
}

/// 面板与 usage 里按 uid 索引的地方都用这个（BTreeMap 保证输出顺序稳定）。
pub fn by_uid(accounts: &[AccountFile]) -> BTreeMap<String, &AccountFile> {
    accounts.iter().map(|a| (a.uid(), a)).collect()
}
