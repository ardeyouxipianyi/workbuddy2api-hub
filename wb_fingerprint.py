"""wb_fingerprint.py —— 统一设备指纹稳定派生模块 (derive_id)

无论是国内版 (copilot.tencent.com / codebuddy.cn) 还是国际版 (www.workbuddy.ai)，
均通过本模块基于账号 UID 和加盐哈希单向派生出固定的伪物理设备特征 (machineId, sessionId, reqId)，
确保每个账号永远来自一台固定的物理设备，且多账号之间天然隔离防风控关联。

形态按 5.8.4 抓包对齐（2026-10-16）：官方客户端的 machineId 取自 OS 硬件 UUID、
sessionId 取自「每进程一个 randomUUID」，两者都是带连字符的 UUID；请求 id 是
32 位无连字符 hex。此前这里输出的是 36 位无连字符 hex 和自造的「前缀-微秒」串，
形态都对不上，本身就是一处「网关特征」。
"""
import hashlib
import time
import uuid

# 官方客户端每进程只生成一次 sessionId（crypto.randomUUID），进程内恒定。
# 网关是长驻进程，所以这里也在模块加载时取一次、全进程复用。
_PROCESS_SESSION_ID = str(uuid.uuid4())


def derive_id(uid: str, salt: str) -> str:
    """由 uid + salt 稳定派生一个带连字符的 UUID 形状设备标识。

    幂等：同一账号每次调用产生相同值，彻底避免随机机器码导致的上游风控。
    版本位与变体位固定成 UUIDv4 的样子——真实客户端的 machineId 就是这个形状。
    """
    seed = f"{salt}:{uid or 'anonymous'}"
    h = hashlib.md5(seed.encode("utf-8")).hexdigest()
    h = h[:12] + "4" + h[13:16] + "a" + h[17:]
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def session_id() -> str:
    """这一进程的 sessionId。

    官方客户端是「每进程一个 randomUUID」而不是从账号派生：一个客户端实例
    的 sessionId 全程不变，换一个实例就换一个。网关同样是长驻进程，照这个
    口径走——进程内恒定、跨进程不同。
    """
    return _PROCESS_SESSION_ID


def generate_request_id(uid: str) -> str:
    """生成 X-Request-ID：32 位无连字符 hex（5.8.4 实测形态）。

    真实客户端的请求 id 与账号无关（每条随机），所以这里不再掺 uid 派生的
    前缀——那反而是一条稳定的「网关特征」。uid 参数保留只为兼容既有调用点。
    """
    return uuid.uuid4().hex


def get_desktop_fingerprint(uid: str, nickname: str = "", os_name: str = "win32") -> dict:
    """生成上报事件所需的标准完整桌面端指纹。"""
    now = int(time.time() * 1000)
    return {
        "timezone": "Asia/Shanghai",
        "reportDelay": 2000,
        "userId": uid,
        "username": nickname,
        "userNickname": nickname,
        "product": "SaaS",
        "releaseDate": 1789036585355,
        "commit": "5f9692923c93033111c51ad7b003eb80204a9b75",
        "ideName": "WorkBuddy",
        "ideType": "WorkBuddy",
        "ideVersion": "5.8.4",
        "machineId": derive_id(uid, "machine"),
        "sessionId": session_id(),
        "extName": "workbuddy-desktop",
        "extVersion": "5.8.4",
        "os": os_name,
        "arch": "x64",
        "osVersion": "10.0.26220",
        "cpuCores": 20,
        "memorySize": 24,
        "timestamp": now,
        "presentAt": now,
    }
