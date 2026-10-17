#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Install one verified WorkBuddy2API-Hub stable release, then health-check it.

This process is started by a root-owned systemd path unit. The gateway can only
write a tiny version request under /run; it cannot run commands as root or
write the application tree itself.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

try:
    import fcntl
except ImportError:  # Allows offline helper-unit fixtures to load on Windows.
    fcntl = None
try:
    import pwd
except ImportError:  # The installed updater runs on Linux; source checks may run elsewhere.
    pwd = None


APP_DIR = "/opt/workbuddy2api-hub"
SERVICE = "workbuddy2api-hub.service"
REQUEST_DIR = "/run/workbuddy2api-hub-updater"
REQUEST_FILE = os.path.join(REQUEST_DIR, "request.json")
STATUS_FILE = os.path.join(REQUEST_DIR, "status.json")
LOCK_FILE = "/run/lock/workbuddy2api-hub-updater.lock"
BACKUP_ROOT = "/var/backups/workbuddy2api-hub"
STAGE_ROOT = "/var/tmp/workbuddy2api-hub-updater"
USER_AGENT = "workbuddy2api-hub-manual-updater"
VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
CHECKSUM_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")
MANIFEST_LIMIT = 1024 * 1024
CHECKSUM_LIMIT = 256 * 1024
ARCHIVE_LIMIT = 128 * 1024 * 1024
UNPACKED_LIMIT = 256 * 1024 * 1024
FILE_LIMIT = 1200
HEALTH_TIMEOUT = 120

BOOTSTRAP_FILES = frozenset((
    "ops/aliyun/install-updater.sh",
    "ops/aliyun/workbuddy2api-hub-updater.py",
    "ops/aliyun/workbuddy2api-hub-updater.service",
    "ops/aliyun/workbuddy2api-hub-updater.path",
    "ops/aliyun/workbuddy2api-hub-updater.conf",
    "ops/aliyun/workbuddy2api-hub-updater-app.conf",
))


class UpdateError(Exception):
    pass


def _log(message):
    print("[wb-update] " + str(message), flush=True)


def _repo():
    value = str(os.environ.get("WB_UPDATE_REPO") or "").strip()
    if not REPO_RE.fullmatch(value):
        raise UpdateError("更新源配置无效")
    return value


def _version_tuple(tag):
    match = VERSION_RE.fullmatch(str(tag or ""))
    if not match:
        return None
    return tuple(int(value) for value in match.groups())


def _wbproxy_ids():
    if pwd is None:
        raise UpdateError("Linux wbproxy account is unavailable")
    try:
        entry = pwd.getpwnam("wbproxy")
    except KeyError as exc:
        raise UpdateError("wbproxy service account is unavailable") from exc
    return entry.pw_uid, entry.pw_gid


def _set_status(state, version="", message=""):
    payload = {
        "state": state,
        "version": str(version or "")[:32],
        "message": str(message or "")[:240],
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    }
    blob = (json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "\n").encode("utf-8")
    uid = 0
    _wbproxy_uid, gid = _wbproxy_ids()
    tmp = os.path.join(REQUEST_DIR, ".status-%d-%d.tmp" % (os.getpid(), time.time_ns()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o640)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    os.replace(tmp, STATUS_FILE)
    dir_fd = os.open(REQUEST_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _safe_exception(exc):
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return "HTTP %d" % code
    if isinstance(exc, UpdateError):
        return str(exc)[:220]
    return type(exc).__name__


def _read_request():
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(REQUEST_FILE, flags)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        expected_uid, _group_id = _wbproxy_ids()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid
                or info.st_size > 2048 or stat.S_IMODE(info.st_mode) & 0o077):
            raise UpdateError("更新请求文件校验失败")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            payload = json.loads(stream.read(2049).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise UpdateError("更新请求格式无效") from exc
    finally:
        os.close(fd)
        try:
            os.unlink(REQUEST_FILE)
        except FileNotFoundError:
            pass

    if not isinstance(payload, dict):
        raise UpdateError("更新请求格式无效")
    tag = payload.get("tag")
    version = payload.get("version")
    parsed = _version_tuple(tag)
    if parsed is None or version != "%d.%d.%d" % parsed:
        raise UpdateError("更新请求版本无效")
    return {"tag": tag, "version": version}


def _fetch(url, limit, timeout=30):
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > limit:
                    raise UpdateError("更新文件超过大小限制")
            except ValueError:
                pass
        chunks = []
        total = 0
        while True:
            block = response.read(min(64 * 1024, limit + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > limit:
                raise UpdateError("更新文件超过大小限制")
        return b"".join(chunks)


def _download_to_file(url, destination, limit, timeout=45):
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Accept": "application/octet-stream"})
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > limit:
                    raise UpdateError("更新文件超过大小限制")
            except ValueError:
                pass
        with open(destination, "xb") as output:
            while True:
                block = response.read(64 * 1024)
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise UpdateError("更新文件超过大小限制")
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
    return digest.hexdigest()


def _release(repo, tag):
    url = "https://api.github.com/repos/%s/releases/tags/%s" % (repo, tag)
    try:
        data = json.loads(_fetch(url, 2 * 1024 * 1024).decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise UpdateError("无法读取更新发布信息") from exc
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        raise UpdateError("目标版本不是已发布的稳定版")
    if str(data.get("tag_name") or "") != tag:
        raise UpdateError("发布版本与请求不匹配")
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("发布包清单无效")
    wanted_zip = "workbuddy2api-hub-%s.zip" % tag
    found = {}
    base = "https://github.com/%s/releases/download/%s/" % (repo, tag)
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        url = item.get("browser_download_url")
        if name not in (wanted_zip, "SHA256SUMS") or not isinstance(url, str):
            continue
        if not url.startswith(base) or urllib.parse.urlparse(url).hostname != "github.com":
            raise UpdateError("发布包下载地址不属于指定仓库")
        found[name] = url
    if wanted_zip not in found or "SHA256SUMS" not in found:
        raise UpdateError("发布包或校验清单缺失")
    return found[wanted_zip], found["SHA256SUMS"]


def _expected_hash(checksum_blob, filename):
    try:
        text = checksum_blob.decode("ascii")
    except UnicodeError as exc:
        raise UpdateError("校验清单格式无效") from exc
    for line in text.splitlines():
        match = CHECKSUM_RE.fullmatch(line)
        if match and match.group(2) == filename:
            return match.group(1).lower()
    raise UpdateError("校验清单没有对应安装包")


def _safe_relative(path):
    if not isinstance(path, str) or not path or "\\" in path or path.startswith("/"):
        return False
    parts = path.split("/")
    return all(part not in ("", ".", "..") and ":" not in part for part in parts)


def _extract_package(archive_path, target_version, target_tag, stage_root):
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError("更新包不是有效 ZIP") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > FILE_LIMIT:
            raise UpdateError("更新包文件数量超过限制")
        manifest_infos = [i for i in infos if i.filename == "release-manifest.json"]
        if len(manifest_infos) != 1 or manifest_infos[0].file_size > MANIFEST_LIMIT:
            raise UpdateError("更新包缺少有效 release-manifest.json")
        manifest_info = manifest_infos[0]
        try:
            manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
        except (UnicodeError, ValueError, KeyError) as exc:
            raise UpdateError("更新包清单格式无效") from exc
        if (not isinstance(manifest, dict) or manifest.get("schema") != 1
                or manifest.get("name") != "workbuddy2api-hub"
                or manifest.get("version") != target_version
                or manifest.get("tag") != target_tag
                or manifest.get("root") != "wb-proxy"):
            raise UpdateError("更新包版本或目录结构不匹配")
        protected = manifest.get("protected")
        if not isinstance(protected, list) or not {"accounts/", "usage/"}.issubset(set(protected)):
            raise UpdateError("更新包没有声明账号与用量保护目录")
        files = manifest.get("files")
        if not isinstance(files, list) or not files or len(files) > FILE_LIMIT:
            raise UpdateError("更新包受管文件清单无效")
        names = set()
        for name in files:
            if not _safe_relative(name) or name in names:
                raise UpdateError("更新包含无效或重复路径")
            if name.startswith(("accounts/", "usage/")):
                raise UpdateError("更新包清单触及受保护目录")
            names.add(name)
        required = BOOTSTRAP_FILES | frozenset((
            "dashboard.html", "wb_proxy.py", "wb_updates.py", "wb_update_request.py"))
        if not required.issubset(names):
            raise UpdateError("更新包缺少更新器或网关文件")

        root = manifest["root"]
        file_map = {}
        total_size = 0
        for info in infos:
            if info.filename == "release-manifest.json":
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise UpdateError("更新包包含符号链接")
            if info.flag_bits & 0x1:
                raise UpdateError("更新包包含加密条目")
            if info.is_dir():
                name = info.filename.rstrip("/")
                if name == root or name.startswith(root + "/"):
                    continue
                raise UpdateError("更新包包含根目录之外的路径")
            if not info.filename.startswith(root + "/"):
                raise UpdateError("更新包包含根目录之外的文件")
            rel = info.filename[len(root) + 1:]
            if not _safe_relative(rel):
                raise UpdateError("更新包包含路径穿越")
            if rel.startswith("python/"):
                # The release ZIP's bundled Python runtime is Windows-only.
                # This systemd install continues to use /usr/bin/python3.
                continue
            if rel not in names:
                raise UpdateError("更新包含未列入清单的程序文件")
            if rel in file_map:
                raise UpdateError("更新包包含重复路径")
            if info.file_size > UNPACKED_LIMIT:
                raise UpdateError("更新包单文件超过大小限制")
            total_size += info.file_size
            if total_size > UNPACKED_LIMIT:
                raise UpdateError("更新包解压总量超过限制")
            file_map[rel] = info
        if set(file_map) != names:
            raise UpdateError("更新包与受管文件清单不一致")

        stage_app = os.path.join(stage_root, "app")
        os.mkdir(stage_app, 0o700)
        extracted = {}
        for name in sorted(names):
            target = os.path.join(stage_app, *name.split("/"))
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            with archive.open(file_map[name]) as src, open(target, "xb") as dst:
                while True:
                    block = src.read(64 * 1024)
                    if not block:
                        break
                    dst.write(block)
            mode = (file_map[name].external_attr >> 16) & 0o777
            extracted[name] = (target, mode or 0o644)
            if name.endswith(".py"):
                try:
                    with open(target, "rb") as fh:
                        compile(fh.read(), name, "exec")
                except (OSError, SyntaxError, UnicodeError) as exc:
                    raise UpdateError("更新包中的 Python 文件无法解析") from exc
        return extracted


def _current_version():
    path = os.path.join(APP_DIR, "wb_proxy.py")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read(2 * 1024 * 1024)
    except OSError as exc:
        raise UpdateError("无法读取当前网关版本") from exc
    match = re.search(r'server_version\s*=\s*"wb-proxy/([^\"]+)"', text)
    if not match:
        raise UpdateError("无法识别当前网关版本")
    return match.group(1)


def _ensure_safe_parent(path, root):
    parent = os.path.dirname(path)
    root_real = os.path.realpath(root)
    parent_real = os.path.realpath(parent)
    if os.path.commonpath((root_real, parent_real)) != root_real:
        raise UpdateError("目标路径越界")
    current = root_real
    relative = os.path.relpath(parent_real, root_real)
    if relative == ".":
        return
    for part in relative.split(os.sep):
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o750)
            _root_uid, group_id = _wbproxy_ids()
            os.chown(current, 0, group_id)
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise UpdateError("目标目录不是普通目录")


def _copy_atomic(source, target, mode, root, group_id):
    _ensure_safe_parent(target, root)
    try:
        info = os.lstat(target)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise UpdateError("目标文件不是普通文件")
    except FileNotFoundError:
        pass
    temp = target + ".wb-update-%d.tmp" % os.getpid()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with open(source, "rb") as src, os.fdopen(fd, "wb", closefd=False) as dst:
            shutil.copyfileobj(src, dst, length=64 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        os.fchown(fd, 0, group_id)
        os.fchmod(fd, mode & 0o755 or 0o640)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
    os.replace(temp, target)


def _is_under_protected(name):
    return name == "accounts" or name.startswith("accounts/") or name == "usage" or name.startswith("usage/")


def _backup_file(source, destination):
    os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
    info = os.lstat(source)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise UpdateError("现有目标文件不是普通文件")
    shutil.copy2(source, destination, follow_symlinks=False)
    os.chmod(destination, stat.S_IMODE(info.st_mode))


def _check_target_paths(files):
    app_info = os.lstat(APP_DIR)
    if (not stat.S_ISDIR(app_info.st_mode) or stat.S_ISLNK(app_info.st_mode)
            or app_info.st_uid != 0 or stat.S_IMODE(app_info.st_mode) & 0o022):
        raise UpdateError("应用目录所有者或类型不符合预期")
    for name in files:
        if _is_under_protected(name):
            raise UpdateError("更新包触及受保护数据")
        if not _safe_relative(name):
            raise UpdateError("更新包路径无效")
        path = os.path.join(APP_DIR, *name.split("/"))
        if os.path.commonpath((os.path.realpath(APP_DIR), os.path.realpath(os.path.dirname(path)))) != os.path.realpath(APP_DIR):
            raise UpdateError("应用目标路径越界")
        parent = APP_DIR
        for part in name.split("/")[:-1]:
            parent = os.path.join(parent, part)
            try:
                parent_info = os.lstat(parent)
            except FileNotFoundError:
                break
            if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
                    or parent_info.st_uid != 0 or stat.S_IMODE(parent_info.st_mode) & 0o022):
                raise UpdateError("程序目标父目录不是普通目录")
        try:
            info = os.lstat(path)
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022):
                raise UpdateError("现有程序路径不是普通文件")
        except FileNotFoundError:
            pass


def _backup_changes(files, backup_dir):
    existed = {}
    for name in files:
        if _is_under_protected(name):
            raise UpdateError("更新包触及受保护数据")
        source = os.path.join(APP_DIR, *name.split("/"))
        try:
            os.lstat(source)
        except FileNotFoundError:
            existed[name] = False
            continue
        destination = os.path.join(backup_dir, "app", *name.split("/"))
        _backup_file(source, destination)
        existed[name] = True
    return existed


def _restore_changes(files, existed, backup_dir, group_id):
    for name in files:
        target = os.path.join(APP_DIR, *name.split("/"))
        if existed.get(name):
            backup = os.path.join(backup_dir, "app", *name.split("/"))
            source_info = os.stat(backup)
            _copy_atomic(backup, target, stat.S_IMODE(source_info.st_mode), APP_DIR, group_id)
        else:
            try:
                os.unlink(target)
            except FileNotFoundError:
                pass


def _systemctl(*args, timeout=60):
    try:
        result = subprocess.run(["/bin/systemctl"] + list(args),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=timeout,
                                check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError("systemd 操作失败") from exc
    if result.returncode:
        raise UpdateError("systemd 操作失败")
    return result.stdout.strip()


def _health_check():
    deadline = time.time() + HEALTH_TIMEOUT
    request = urllib.request.Request(
        "http://127.0.0.1:8788/health", headers={"User-Agent": USER_AGENT})
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                if response.status == 200:
                    data = json.loads(response.read(8192).decode("utf-8"))
                    if isinstance(data, dict) and data.get("ok") is True:
                        return True
        except (OSError, urllib.error.URLError, UnicodeError, ValueError):
            pass
        time.sleep(2)
    return False


def _do_update(request_data):
    if getattr(os, "geteuid", lambda: -1)() != 0:
        raise UpdateError("更新服务必须由 systemd 管理员服务运行")
    repo = _repo()
    tag = request_data["tag"]
    target_version = request_data["version"]
    target_tuple = _version_tuple(tag)
    current_version = _current_version()
    current_tuple = tuple(int(value) for value in current_version.split("."))
    if target_tuple is None or target_tuple <= current_tuple:
        raise UpdateError("目标版本无效或不高于当前版本")

    os.makedirs(BACKUP_ROOT, mode=0o700, exist_ok=True)
    os.chmod(BACKUP_ROOT, 0o700)
    os.makedirs(STAGE_ROOT, mode=0o700, exist_ok=True)
    os.chmod(STAGE_ROOT, 0o700)
    _wbproxy_uid, group_id = _wbproxy_ids()
    backup_name = time.strftime("backup-%Y%m%d-%H%M%S", time.localtime()) + "-%d" % os.getpid()
    backup_dir = os.path.join(BACKUP_ROOT, backup_name)
    os.mkdir(backup_dir, 0o700)
    stage_root = tempfile.mkdtemp(prefix="stage-", dir=STAGE_ROOT)
    stopped = False
    existed = None
    managed_files = []
    try:
        _set_status("downloading", target_version, "正在读取稳定版发布信息")
        zip_url, sums_url = _release(repo, tag)
        archive_name = "workbuddy2api-hub-%s.zip" % tag
        sums = _fetch(sums_url, CHECKSUM_LIMIT)
        expected = _expected_hash(sums, archive_name)
        archive_path = os.path.join(stage_root, archive_name)
        actual = _download_to_file(zip_url, archive_path, ARCHIVE_LIMIT)
        if actual != expected:
            raise UpdateError("更新包 SHA-256 校验失败")

        _set_status("verifying", target_version, "正在校验发布清单与文件路径")
        extracted = _extract_package(archive_path, target_version, tag, stage_root)
        managed_files = sorted(extracted)
        _check_target_paths(managed_files)

        _set_status("backing_up", target_version, "正在备份程序文件")
        existed = _backup_changes(managed_files, backup_dir)
        with open(os.path.join(backup_dir, "restore-state.json"), "w", encoding="utf-8") as fh:
            json.dump(existed, fh, ensure_ascii=False, separators=(",", ":"))
            fh.write("\n")
        os.chmod(os.path.join(backup_dir, "restore-state.json"), 0o600)

        _set_status("stopping", target_version, "正在停止网关")
        stopped = True
        _systemctl("stop", SERVICE)

        _set_status("applying", target_version, "正在安装程序文件")
        for name in managed_files:
            source, mode = extracted[name]
            target = os.path.join(APP_DIR, *name.split("/"))
            _copy_atomic(source, target, mode, APP_DIR, group_id)
        _set_status("restarting", target_version, "正在启动网关")
        _systemctl("start", SERVICE)
        _set_status("checking", target_version, "正在检查网关健康状态")
        if not _health_check():
            raise UpdateError("更新后网关健康检查未通过")
        stopped = False
        _set_status("succeeded", target_version, "更新完成；账号与用量目录保持原样")
        _log("installed stable release %s; backup=%s" % (target_version, backup_dir))
    except Exception as exc:
        message = _safe_exception(exc)
        if stopped and existed is not None:
            try:
                _set_status("rolling_back", target_version, "更新失败，正在恢复备份")
                _systemctl("stop", SERVICE)
                _restore_changes(managed_files, existed, backup_dir, group_id)
                _systemctl("start", SERVICE)
                if not _health_check():
                    raise UpdateError("恢复后的网关健康检查未通过")
                _set_status("rolled_back", target_version,
                            "更新失败，已恢复原程序；原因：" + message)
                _log("update rolled back; backup=%s" % backup_dir)
            except Exception as rollback_exc:
                rollback_message = _safe_exception(rollback_exc)
                _set_status("failed", target_version,
                            "自动回滚未能确认成功；保留备份。更新错误：%s；回滚错误：%s"
                            % (message, rollback_message))
                _log("update and rollback both failed; backup=%s" % backup_dir)
        else:
            _set_status("failed", target_version, message)
            _log("update failed before service replacement: %s" % message)
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def main():
    if os.name != "posix" or fcntl is None:
        _log("the updater service is supported on Linux only")
        return 1
    _wbproxy_uid, wbproxy_gid = _wbproxy_ids()
    try:
        request_dir_info = os.lstat(REQUEST_DIR)
        lock_dir_info = os.stat(os.path.dirname(LOCK_FILE))
    except OSError:
        _log("updater request directory is unavailable")
        return 1
    if (not stat.S_ISDIR(request_dir_info.st_mode)
            or stat.S_ISLNK(request_dir_info.st_mode)
            or request_dir_info.st_uid != 0
            or request_dir_info.st_gid != wbproxy_gid
            or not (stat.S_IMODE(request_dir_info.st_mode) & stat.S_ISVTX)
            or (stat.S_IMODE(request_dir_info.st_mode) & 0o037) != 0o030
            or not stat.S_ISDIR(lock_dir_info.st_mode)):
        _log("updater request directory permissions are invalid")
        return 1
    lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(lock_fd, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _set_status("queued", "", "已有更新服务正在运行")
        return 1
    try:
        request_data = _read_request()
        if request_data is None:
            return 0
        _set_status("queued", request_data["version"], "更新任务已排队")
        try:
            _do_update(request_data)
            return 0
        except Exception:
            return 1
    except Exception as exc:
        _set_status("failed", "", _safe_exception(exc))
        _log("request rejected: %s" % _safe_exception(exc))
        return 1
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
