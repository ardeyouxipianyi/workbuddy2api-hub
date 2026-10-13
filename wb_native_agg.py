# -*- coding: utf-8 -*-
"""wb_native_agg —— wb-agg 聚合引擎的可选进程内加速件（标准 C ABI，ctypes 绑定）。

定位
====
这是**可选**加速件：libwb_agg.so 缺失、加载失败、ABI 版本不符，或任何一次
调用出错时，available() 返回 False、run()/run_text() 返回 None，调用方静默
回退纯 Python 折叠。本模块绝不把异常抛给调用方——引擎不能把网关带崩。
纯标准库（ctypes/json/os/threading），不新增任何第三方依赖；Python 3.9 兼容
（不用 match、不用 X|Y 注解、不用 3.10+ API）。

库定位顺序
==========
1. 环境变量 WB_AGG_LIB（给了就先试它）；
2. 与脚本同目录的库文件（libwb_agg.so / libwb_agg.dylib / wb_agg.dll，
   按平台惯例都试一遍，native/build.py 按同一组名字产出）；
3. 都没有 / 都加载失败 → available() 为 False。
失败只尝试一次并记住结果（懒加载 + 一把锁包住首次加载，线程安全）；
已加载成功后 run() 不再取锁。

C 侧 ABI（本模块按此绑定）
==========================
    const char *wb_agg_version(void);                        /* 必须是 "1" */
    char *wb_agg_run_json(const char *request, size_t len);  /* 成功: malloc 的 JSON 文本; 失败: NULL */
    void wb_agg_release(char *result);                       /* 释放上面的返回值 */
    const char *wb_agg_error(void);                          /* 最近一次错误文本（诊断用） */

用法
====
    import wb_native_agg
    if wb_native_agg.available():
        payload = wb_native_agg.run({"log": "...", "sections": [...]})
        if payload is None:
            payload = python_fallback()      # 静默回退
    else:
        payload = python_fallback()
"""

import ctypes
import json
import os
import threading

_ABI_VERSION = "1"
# 与脚本同目录的库文件名，按平台惯例都试一遍（native/build.py 按同一组名字产出）
_LIB_FILENAMES = ("libwb_agg.so", "libwb_agg.dylib", "wb_agg.dll")

_lock = threading.Lock()
_attempted = False
_library = None
_library_path = None
_version = None


def _candidate_paths():
    """库候选路径，按定位顺序：WB_AGG_LIB → 与脚本同目录的库文件。"""
    paths = []
    env = os.environ.get("WB_AGG_LIB")
    if env:
        paths.append(env)
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        here = ""
    if here:
        for name in _LIB_FILENAMES:
            sibling = os.path.join(here, name)
            if sibling not in paths and os.path.isfile(sibling):
                paths.append(sibling)
    return paths


def _bind(lib):
    """绑定四个导出符号并校验 ABI 版本；任何一步不成立返回 None（不抛）。"""
    try:
        lib.wb_agg_version.argtypes = []
        lib.wb_agg_version.restype = ctypes.c_char_p
        lib.wb_agg_run_json.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        lib.wb_agg_run_json.restype = ctypes.c_void_p
        lib.wb_agg_release.argtypes = [ctypes.c_void_p]
        lib.wb_agg_release.restype = None
        lib.wb_agg_error.argtypes = []
        lib.wb_agg_error.restype = ctypes.c_char_p
        raw = lib.wb_agg_version()
    except Exception:
        return None
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8", "replace").strip()
    except Exception:
        return None
    if text != _ABI_VERSION:
        return None
    return text


def _load():
    """懒加载：返回已绑定的 CDLL，或 None。失败被记住，不重复尝试。"""
    global _attempted, _library, _library_path, _version
    with _lock:
        if _attempted:
            return _library
        _attempted = True
        for path in _candidate_paths():
            try:
                lib = ctypes.CDLL(path)
            except Exception:
                continue
            try:
                version = _bind(lib)
            except Exception:
                version = None
            if version is None:
                continue
            _library = lib
            _library_path = path
            _version = version
            break
        return _library


def available():
    """库可用（已加载且 ABI 版本为 "1"）时 True。绝不抛异常。"""
    try:
        return _load() is not None
    except Exception:
        return False


def version():
    """已加载库的 ABI 版本字符串；不可用时 None。绝不抛异常。"""
    try:
        _load()
    except Exception:
        pass
    return _version


def library_path():
    """已加载库的路径；不可用时 None（诊断用）。绝不抛异常。"""
    try:
        _load()
    except Exception:
        pass
    return _library_path


def _invoke(request):
    """调引擎跑一次，返回结果 JSON 的 bytes；任何失败返回 None。绝不抛异常。"""
    lib = _load()
    if lib is None:
        return None
    try:
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    except Exception:
        return None
    ptr = None
    try:
        ptr = lib.wb_agg_run_json(payload, len(payload))
        if not ptr:
            return None
        return ctypes.string_at(ptr)
    except Exception:
        return None
    finally:
        if ptr:
            try:
                lib.wb_agg_release(ptr)
            except Exception:
                pass


def run(request):
    """跑一次聚合，返回解析后的 dict；不可用/失败/非 dict 返回 None。绝不抛异常。"""
    raw = _invoke(request)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def run_text(request):
    """跑一次聚合，返回引擎输出的 JSON 文本；不可用/失败返回 None。绝不抛异常。"""
    raw = _invoke(request)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8", "replace")
    except Exception:
        return None


def last_error():
    """引擎最近一次错误的文本（诊断用）；拿不到返回 None。绝不抛异常。"""
    lib = _load()
    if lib is None:
        return None
    try:
        raw = lib.wb_agg_error()
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return raw.decode("utf-8", "replace")
    except Exception:
        return None
