# -*- coding: utf-8 -*-
"""wb_usage_io.py —— 把一行 JSONL 原子地追加进账本。

## 为什么单独一个模块

`usage.jsonl` 是账本：每一行都是一次真实调用。它可能在**多个进程**里被同时追加
（网关本体 + 维护脚本 + 面板导出任务）。原先的写法是

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")

文本模式按缓冲区切分，一次 `.write()` 会落成多个 `write(2)`，两个人一起写就会拼出
半行（实测出现过一条 `9.8}`）。实测两种表现：

- 4 个进程各写 120 行：480 行只落进去 408~430 行；
- 单进程 8 线程各写 300 行：2400 行只落进去 2224 行（丢 7.3%）——**这才是网关的
  真实形态**（每个请求各写一行）。

丢行比坏行更危险：读的一侧全部 `except: continue`，所以丢掉的调用既不在请求数里、
也不在积分合计里，账面上直接少一笔，且没有任何报错。

## 做法

1. **单次 `os.write`**：整行先编码成 bytes，再一次系统调用写完，不给交叉留缝隙；
2. **`O_APPEND`**：由内核把写入位置定在末尾。POSIX 明确保证"设置偏移到末尾"与
   "写入"之间不会插入别的修改，所以**单次 write 在 POSIX 上已经是原子的**，
   这里不再额外加 flock（也就避开了 `fcntl`——它在 Windows 上不存在，
   而便携运行时的白名单里没有它）；
3. **Windows 额外加锁**：`_O_APPEND` 走 CRT，并发句柄之间不保证原子，用
   `msvcrt.locking` 锁一个独立的 `.lock` 文件（不锁数据文件，避免干扰读的一侧）；
4. **进程内线程锁**：同进程多线程先串起来，减少跨进程争抢。
"""
import os
import threading

_LOCK_SUFFIX = ".lock"
_THREAD_LOCK = threading.Lock()

try:                                                    # Windows
    import msvcrt

    def _lock(lock_fd):
        # LK_LOCK 重试 10 秒后抛 OSError；账本争抢是毫秒级的，够用。
        while True:
            try:
                msvcrt.locking(lock_fd, msvcrt.LK_LOCK, 1)
                return
            except OSError:
                # 拿不到就继续等：这里没有别的退路，放弃等于丢账。
                continue

    def _unlock(lock_fd):
        try:
            msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

except ImportError:                                     # POSIX：单次 write 已原子
    def _lock(lock_fd):
        return None

    def _unlock(lock_fd):
        return None


def append_line(path, text, error_hook=None):
    """把 `text`（一行，不含换行）原子地追加到 `path`，返回写入字节数。

    失败时调 `error_hook(message)` 而不是抛出：账本写不进去不该让请求失败
    （调用方原本也是 try/except 包着的），但必须留痕。
    """
    payload = (text.rstrip("\n") + "\n").encode("utf-8")
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with _THREAD_LOCK:
            lock_fd = os.open(path + _LOCK_SUFFIX, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                _lock(lock_fd)
                try:
                    # O_APPEND：内核负责定位到末尾；单次 write 不给交错留缝。
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                    try:
                        written = 0
                        while written < len(payload):
                            written += os.write(fd, payload[written:])
                    finally:
                        os.close(fd)
                finally:
                    _unlock(lock_fd)
            finally:
                os.close(lock_fd)
    except Exception as exc:                            # noqa: BLE001
        if error_hook is not None:
            error_hook(str(exc))
        return 0
    return len(payload)
