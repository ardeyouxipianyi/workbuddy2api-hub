# -*- coding: utf-8 -*-
"""Panel-side request/status bridge for the privileged update service.

The gateway process stays unprivileged. It can create one validated request in
a root-owned sticky directory and read status files written by the updater.
It never runs systemctl or writes application files itself.
"""

import json
import os
import re
import stat
import time


REQUEST_DIR = os.environ.get(
    "WB_UPDATE_REQUEST_DIR", "/run/workbuddy2api-hub-updater")
REQUEST_FILE = os.path.join(REQUEST_DIR, "request.json")
STATUS_FILE = os.path.join(REQUEST_DIR, "status.json")
PATH_UNIT_FILE = "/etc/systemd/system/workbuddy2api-hub-updater.path"
ENABLED_UNIT_LINK = ("/etc/systemd/system/multi-user.target.wants/"
                     "workbuddy2api-hub-updater.path")
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
ACTIVE_STATES = frozenset((
    "queued", "downloading", "verifying", "backing_up", "stopping",
    "applying", "restarting", "checking", "rolling_back",
))
VISIBLE_STATES = frozenset((
    "idle", "queued", "downloading", "verifying", "backing_up",
    "stopping", "applying", "restarting", "checking", "succeeded",
    "rolling_back", "rolled_back", "failed",
))
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class UpdateRequestError(Exception):
    """A safe, user-visible reason an install request was refused."""


def install_enabled():
    """Only enable the panel action after the administrator installs its unit."""
    return (
        str(os.environ.get("WB_UPDATE_INSTALL_ENABLED") or "").strip() == "1"
        and os.path.isdir(REQUEST_DIR)
        and os.path.exists(PATH_UNIT_FILE)
        and os.path.exists(ENABLED_UNIT_LINK)
    )


def _version_tuple(tag):
    match = TAG_RE.fullmatch(str(tag or "").strip())
    return tuple(int(value) for value in match.groups()) if match else None


def _read_root_status():
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _NOFOLLOW
    try:
        fd = os.open(STATUS_FILE, flags)
    except FileNotFoundError:
        return {"state": "idle", "version": "", "message": ""}
    except OSError:
        return {"state": "unavailable", "version": "", "message": ""}
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_uid", -1) != 0
                or info.st_size > 8192 or (stat.S_IMODE(info.st_mode) & 0o022)):
            return {"state": "unavailable", "version": "", "message": ""}
        with os.fdopen(fd, "rb", closefd=False) as stream:
            payload = json.loads(stream.read(8193).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return {"state": "unavailable", "version": "", "message": ""}
    finally:
        os.close(fd)

    if not isinstance(payload, dict):
        return {"state": "unavailable", "version": "", "message": ""}
    state = str(payload.get("state") or "")
    if state not in VISIBLE_STATES:
        return {"state": "unavailable", "version": "", "message": ""}
    version = str(payload.get("version") or "")[:32]
    message = str(payload.get("message") or "")[:240]
    updated_at = str(payload.get("updated_at") or "")[:32]
    result = {"state": state, "version": version, "message": message}
    if updated_at:
        result["updated_at"] = updated_at
    return result


def status():
    result = {"install_enabled": install_enabled(),
              "installation": _read_root_status()}
    current = result["installation"]
    if (os.path.exists(REQUEST_FILE)
            and current.get("state") not in ACTIVE_STATES):
        result["installation"] = {
            "state": "queued", "version": current.get("version", ""),
            "message": "",
        }
    return result


def request_install(tag, current_version):
    """Queue a stable tag for the root updater; caller must panel-authenticate."""
    if not install_enabled():
        raise UpdateRequestError("安装更新服务尚未配置")
    target = _version_tuple(tag)
    current = _version_tuple("v" + str(current_version or "").strip().lstrip("v"))
    if target is None or current is None or target <= current:
        raise UpdateRequestError("目标版本无效或不高于当前版本")

    existing = status()["installation"]
    if existing.get("state") in ACTIVE_STATES or os.path.exists(REQUEST_FILE):
        raise UpdateRequestError("已有更新任务正在运行")

    payload = json.dumps({
        "tag": tag,
        "version": "%d.%d.%d" % target,
        "requested_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    }, separators=(",", ":")).encode("utf-8")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | _NOFOLLOW)
    try:
        fd = os.open(REQUEST_FILE, flags, 0o600)
    except FileExistsError:
        raise UpdateRequestError("已有更新任务正在运行")
    except OSError:
        raise UpdateRequestError("无法提交更新任务，请检查更新服务状态")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        try:
            os.unlink(REQUEST_FILE)
        except OSError:
            pass
        raise UpdateRequestError("提交更新任务失败")
    return {"state": "queued", "version": "%d.%d.%d" % target,
            "message": "更新任务已排队"}
