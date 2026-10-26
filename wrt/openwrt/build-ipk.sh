#!/bin/sh
# ============================================================================
# WorkBuddy2API-Hub · OpenWrt 版 —— .ipk 打包脚本（opkg 系，OpenWrt <= 24.10）
#
# 用法：
#   sh wrt/openwrt/build-ipk.sh [--arch <arch>] [--app-src <目录>]
#
# 产物（写到 wrt/ipk/ 目录）：
#   wrt/ipk/workbuddy2api_<版本>-<发布号>_all.ipk
#
# 为什么默认 Architecture: all
#   本包**全部是脚本**：wb_*.py（纯标准库 Python）+ dashboard.html + ash 服务脚本 +
#   uci 配置 —— 没有任何架构相关二进制。所以一个 all 包在 x86_64 / aarch64 /
#   armv7a 等架构上通用，不需要为每个架构各打一份。--arch 只改 control 的
#   Architecture 字段与产物文件名，给「按架构分目录的私有源」这类场景用。
#
# 依赖：POSIX sh + python3（只用标准库）。
#   不需要 OpenWrt SDK，不需要 ar/tar/gzip；路由器（busybox ash）或任意
#   Linux/macOS 上都能直接跑。Windows 上要先有 POSIX sh（WSL / Git Bash）。
#
# 包格式（与 OpenWrt 官方 scripts/ipkg-build 的输出一致，别自创）：
#   gzip( tar( ./debian-binary  ./data.tar.gz  ./control.tar.gz ) )
#   * 外层是 gzip 压缩的 tar，**不是 ar** —— 自 18.06 起 OpenWrt 官方源里的
#     .ipk 就都是这个格式（实测 18.06.9 / 19.07.10 / 21.02.7 / 22.03.7 /
#     23.05.5 / 24.10.2 全是 gzip 魔数；opkg 走 libarchive，两种容器都认）。
#   * 两个内层 tar 都是 GNU tar 格式：uid/gid=0、uname/gname 为空、按文件名
#     排序、mtime 统一取 SOURCE_DATE_EPOCH（默认 1700000000，保证可复现）。
#   * control.tar.gz 里按需放：./control（必需）、./conffiles、./postinst、
#     ./prerm、./postrm。
#   * data.tar.gz 里的路径带前缀 ./（如 ./usr/bin/workbuddy2api-warm）。
#
# 源文件：应用源码（wb_*.py + dashboard.html）与 build-apk.sh 同源——都取自
#   --app-src（默认就是本仓库检出），不手工复制内容，也不从网络拉取。元数据
#   （版本 / 发布号 / 许可 / 维护者 / 依赖 / 描述）从 openwrt/*/Makefile 解析，
#   避免两处维护；版本号本身以 wb_proxy.py 为准，与 Makefile 对不上就拒绝打包。
#
# 幂等：重复运行覆盖 ipk/ 下同名产物；同一份源码 + 同一个 SOURCE_DATE_EPOCH
#   得到逐字节相同的包。
# ============================================================================
set -e

usage() {
	cat <<'EOF'
用法: sh wrt/openwrt/build-ipk.sh [--arch <arch>] [--app-src <目录>]

  --arch <arch>    写进 control 的 Architecture 字段与产物文件名（默认 all）。
                   纯脚本包用 all 即可（x86_64 / aarch64 / armv7a 通用）。
  --app-src <目录> 含 wb_*.py + dashboard.html 的目录（可选，默认本仓库检出）。
  -h, --help       显示本帮助

产物: wrt/ipk/workbuddy2api_<版本>-<发布号>_<架构>.ipk
EOF
}

ARCH=all
APP_SRC=""
while [ $# -gt 0 ]; do
	case "$1" in
		--arch)    [ -n "$2" ] || { echo "错误: --arch 需要一个参数" >&2; exit 1; }; ARCH="$2"; shift 2 ;;
		--arch=*)  ARCH="${1#--arch=}"; shift ;;
		--app-src) [ -n "$2" ] || { echo "错误: --app-src 需要一个参数" >&2; exit 1; }; APP_SRC="$2"; shift 2 ;;
		--app-src=*) APP_SRC="${1#--app-src=}"; shift ;;
		-h|--help) usage; exit 0 ;;
		*) echo "错误: 未知参数 '$1'" >&2; usage >&2; exit 1 ;;
	esac
done

case "$ARCH" in
	''|*[!A-Za-z0-9_.+-]*)
		echo "错误: 非法的架构名 '$ARCH'（只允许字母、数字与 _ . + -）" >&2
		exit 1
		;;
esac

# REPO 是 wrt/ 目录（本脚本住在 wrt/openwrt/ 下）：产物落 wrt/ipk，
# 包目录是 wrt/openwrt/workbuddy2api，应用源码默认取仓库根。
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

PY=
for _c in python3 python; do
	if command -v "$_c" >/dev/null 2>&1; then PY="$_c"; break; fi
done
[ -n "$PY" ] || { echo "错误: 找不到 python3（打包只需要它的标准库）" >&2; exit 1; }

# 可复现构建用的统一时间戳（同 OpenWrt 的 SOURCE_DATE_EPOCH 约定）
EPOCH="${SOURCE_DATE_EPOCH:-1700000000}"
case "$EPOCH" in
	''|*[!0-9]*) echo "错误: SOURCE_DATE_EPOCH 必须是整数秒" >&2; exit 1 ;;
esac

# ---- 从 Makefile 解析元数据（单一事实来源） --------------------------------

# mk_var <makefile> <VAR> -> 变量值（取第一处赋值，允许行首缩进）
mk_var() {
	sed -n "s/^[[:space:]]*$2:=[[:space:]]*//p" "$1" | sed -n 1p
}

# mk_depends <makefile> -> "a, b, c"（把 Makefile 的 "+a +b" 转成 ipk 的 Depends）
mk_depends() {
	_deps=
	for _d in $(sed -n 's/^[[:space:]]*DEPENDS:=[[:space:]]*//p' "$1" | sed -n 1p); do
		_d=${_d#+}
		[ -n "$_d" ] || continue
		_deps="${_deps}${_deps:+, }${_d}"
	done
	printf '%s' "$_deps"
}

# mk_description <makefile> <pkgname> -> 描述正文（define .../description 块，去行首缩进）
mk_description() {
	sed -n "/^define Package\/$2\/description/,/^endef/p" "$1" | sed -e '1d' -e '$d'
}

# write_control <makefile> <pkgname> <section> <source> <provides> <outfile>
write_control() {
	_wc_mk="$1"; _wc_name="$2"; _wc_section="$3"; _wc_source="$4"; _wc_provides="$5"; _wc_out="$6"
	_wc_ver=$(mk_var "$_wc_mk" PKG_VERSION)
	_wc_rel=$(mk_var "$_wc_mk" PKG_RELEASE)
	_wc_license=$(mk_var "$_wc_mk" PKG_LICENSE)
	_wc_maint=$(mk_var "$_wc_mk" PKG_MAINTAINER)
	_wc_url=$(mk_var "$_wc_mk" URL)
	_wc_depends=$(mk_depends "$_wc_mk")

	[ -n "$_wc_ver" ] || { echo "错误: 从 $_wc_mk 解析不出 PKG_VERSION" >&2; exit 1; }
	[ -n "$_wc_rel" ] || { echo "错误: 从 $_wc_mk 解析不出 PKG_RELEASE" >&2; exit 1; }
	[ -n "$_wc_depends" ] || { echo "错误: 从 $_wc_mk 解析不出 DEPENDS" >&2; exit 1; }
	[ -n "$(mk_description "$_wc_mk" "$_wc_name")" ] || {
		echo "错误: 从 $_wc_mk 解析不出 Package/$_wc_name/description" >&2; exit 1; }

	{
		printf 'Package: %s\n' "$_wc_name"
		printf 'Version: %s-%s\n' "$_wc_ver" "$_wc_rel"
		printf 'Depends: %s\n' "$_wc_depends"
		if [ -n "$_wc_provides" ]; then printf 'Provides: %s\n' "$_wc_provides"; fi
		printf 'Source: %s\n' "$_wc_source"
		printf 'SourceName: %s\n' "$_wc_name"
		if [ -n "$_wc_license" ]; then printf 'License: %s\n' "$_wc_license"; fi
		printf 'Section: %s\n' "$_wc_section"
		printf 'SourceDateEpoch: %s\n' "$EPOCH"
		if [ -n "$_wc_url" ]; then printf 'URL: %s\n' "$_wc_url"; fi
		if [ -n "$_wc_maint" ]; then printf 'Maintainer: %s\n' "$_wc_maint"; fi
		printf 'Architecture: %s\n' "$ARCH"
		# 占位，打包时由 python 改成 data.tar.gz 解压后的大小（24.10 的算法）
		printf 'Installed-Size: 0\n'
		# Description：首行 "Description: " + 每行行首一个空格（OpenWrt 的续行约定）
		printf 'Description: '
		mk_description "$_wc_mk" "$_wc_name" | sed -e 's/^[[:space:]]*/ /'
	} > "$_wc_out"
}

# ---- 应用源码（与 build-apk.sh 同源） ---------------------------------------

echo "==> 1/4 准备应用源码"
if [ -z "$APP_SRC" ]; then
	APP_SRC=$(CDPATH= cd -- "$REPO/.." && pwd)
	echo "    未指定 --app-src，按仓库检出取: $APP_SRC"
fi
[ -f "$APP_SRC/wb_proxy.py" ] || {
	echo "错误: $APP_SRC 里没有 wb_proxy.py（--app-src 应指向含 wb_*.py 的仓库根）" >&2; exit 1; }
[ -f "$APP_SRC/dashboard.html" ] || { echo "错误: $APP_SRC 里没有 dashboard.html" >&2; exit 1; }
DETECTED_VER="$(sed -n 's/.*server_version = "wb-proxy\/\([0-9.]*\)".*/\1/p' "$APP_SRC/wb_proxy.py" | head -n1)"
MAKE_VER="$(mk_var "$REPO/openwrt/workbuddy2api/Makefile" PKG_VERSION)"
[ -n "$DETECTED_VER" ] || {
	echo "错误: 从 $APP_SRC/wb_proxy.py 解析不出 server_version" >&2; exit 1; }
[ "$DETECTED_VER" = "$MAKE_VER" ] || {
	echo "错误: 源码版本（$DETECTED_VER）与 Makefile PKG_VERSION（${MAKE_VER:-未知}）不一致。" >&2
	echo "      升级时请把 wrt/openwrt/workbuddy2api/Makefile 的 PKG_VERSION 改成 $DETECTED_VER。" >&2
	exit 1
}
echo "    源码版本 $DETECTED_VER（$(ls "$APP_SRC"/wb_*.py | wc -l | tr -d ' ') 个 py + dashboard.html）"

# ---- 暂存目录 ---------------------------------------------------------------

WORK=$(mktemp -d "${TMPDIR:-/tmp}/build-ipk.XXXXXX")
trap 'rm -rf "$WORK"' EXIT INT TERM

DATA="$WORK/data-workbuddy2api"
CTRL="$WORK/control-workbuddy2api"

# install_file <源文件> <暂存根> <安装路径> <权限>
install_file() {
	[ -f "$1" ] || { echo "错误: 找不到源文件 $1" >&2; exit 1; }
	mkdir -p "$(dirname "$2/$3")"
	cp "$1" "$2/$3"
	chmod "$4" "$2/$3"
}

echo "==> 2/4 暂存包内文件"
PKG="$REPO/openwrt/workbuddy2api"
install_file "$PKG/files/etc/init.d/workbuddy2api"  "$DATA" etc/init.d/workbuddy2api  0755
install_file "$PKG/files/etc/config/workbuddy2api"  "$DATA" etc/config/workbuddy2api  0600
install_file "$PKG/files/usr/bin/workbuddy2api-warm"   "$DATA" usr/bin/workbuddy2api-warm   0755
# 应用源码：遍历目录，别写死文件名（上游新增模块时不会漏打包）
for _f in "$APP_SRC"/wb_*.py; do
	install_file "$_f" "$DATA" "usr/lib/workbuddy2api/$(basename "$_f")" 0644
done
install_file "$APP_SRC/dashboard.html" "$DATA" usr/lib/workbuddy2api/dashboard.html 0644
for _f in "$APP_SRC"/assets/css/*.css "$APP_SRC"/assets/js/*.js; do
	install_file "$_f" "$DATA" "usr/lib/workbuddy2api/${_f#"$APP_SRC"/}" 0644
done

mkdir -p "$CTRL"
write_control "$PKG/Makefile" workbuddy2api net feeds/base/workbuddy2api "" "$CTRL/control"
printf '/etc/config/workbuddy2api\n' > "$CTRL/conffiles"
chmod 0644 "$CTRL/control" "$CTRL/conffiles"

# postinst：与 apk 包语义一致（真 ipk 的标准开头 + default_postinst，再跟自定义代码）。
# 注意 opkg 的 default_postinst 同样会对包里的 /etc/init.d/* 先 enable 再 start
# （见 /lib/functions.sh），所以首次安装要显式 disable + stop 一次；升级不动用户状态。
cat > "$CTRL/postinst" <<'EOF'
#!/bin/sh
[ "${IPKG_NO_SCRIPT}" = "1" ] && exit 0
[ -s ${IPKG_INSTROOT}/lib/functions.sh ] || exit 0
. ${IPKG_INSTROOT}/lib/functions.sh
default_postinst $0 $@
[ -n "${IPKG_INSTROOT}" ] || {
	mkdir -p /etc/workbuddy2api/accounts /etc/workbuddy2api/usage
	chmod 700 /etc/workbuddy2api /etc/workbuddy2api/accounts
	[ -f /etc/config/workbuddy2api ] && chmod 600 /etc/config/workbuddy2api
	grep -q '^/etc/workbuddy2api$' /etc/sysupgrade.conf 2>/dev/null || \
		echo '/etc/workbuddy2api' >> /etc/sysupgrade.conf
	# 缓存预热：每 2 分钟把面板的贵接口（/usage 汇总、统计页矩阵、analytics/
	# timeseries 时间窗、面板轮询的 realm 接口）先跑一遍。**缓存命中不会延长
	# TTL**，条目从上次重建起算过期，所以预热间隔必须显著小于 TTL，否则
	# 「已过期但还没重建」的窗口里用户点开面板就要等全表扫描（TTL 900 + 2
	# 分钟一轮 → 窗口约 6%，而全部命中时一轮只有毫秒级成本）。uci main.warm=0 可关。
	# 这一行按规范形式重写（老版本装的是 */12，直接 sed 掉，保证升级后也对齐）
	mkdir -p /etc/crontabs
	sed -i '/workbuddy2api-warm/d' /etc/crontabs/root 2>/dev/null
	echo '*/2 * * * * /usr/bin/workbuddy2api-warm --quiet' >> /etc/crontabs/root
	/etc/init.d/cron restart >/dev/null 2>&1 || true
	[ "${PKG_UPGRADE}" = "1" ] || {
		/etc/init.d/workbuddy2api disable >/dev/null 2>&1
		/etc/init.d/workbuddy2api stop >/dev/null 2>&1
	}
}
# init.d 的 disable/stop 失败不该让 opkg 报 postinst 失败（apk 版没有这行，语义不变）
exit 0
EOF

# prerm：与真 ipk 生成的一模一样（default_prerm 会 stop + disable 服务）
cat > "$CTRL/prerm" <<'EOF'
#!/bin/sh
[ -s ${IPKG_INSTROOT}/lib/functions.sh ] || exit 0
. ${IPKG_INSTROOT}/lib/functions.sh
default_prerm $0 $@
EOF

# postrm：清理包自己的文件（与 apk 包的 postrm 一致）；
# /etc/workbuddy2api（账号/用量）与 /etc/config/workbuddy2api 是用户数据，保留。
cat > "$CTRL/postrm" <<'EOF'
#!/bin/sh
[ -n "${IPKG_INSTROOT}" ] || {
	rm -rf /usr/lib/workbuddy2api
	rm -f /usr/bin/workbuddy2api-warm
	sed -i '/workbuddy2api-warm/d' /etc/crontabs/root 2>/dev/null
	/etc/init.d/cron restart >/dev/null 2>&1 || true
}
EOF

chmod 0755 "$CTRL/postinst" "$CTRL/prerm" "$CTRL/postrm"

# ---- 打包 -------------------------------------------------------------------

# 内嵌的 python 打包器（只用标准库）：写临时文件跑，保证脚本自包含。
PY_HELPER="$WORK/ipkpack.py"
cat > "$PY_HELPER" <<'PYEOF'
#!/usr/bin/env python3
"""OpenWrt .ipk 打包/自检（与官方 scripts/ipkg-build 的输出结构一致）。

pack   <data_dir> <control_dir> <out.ipk> <epoch>
verify <file.ipk>
"""
import gzip
import hashlib
import io
import os
import re
import stat
import sys
import tarfile

GZ_LEVEL = 6  # 同 OpenWrt 的 `gzip -n -` 默认级别（6），连 gzip 头都一致


def gz(data):
    """gzip：mtime=0、级别 6（同 gzip -n），OS 字节写成 3（Unix，同 OpenWrt 产物）。"""
    out = gzip.compress(data, compresslevel=GZ_LEVEL, mtime=0)
    return out[:9] + b"\x03" + out[10:]


def tar_members(root):
    """递归收集 root 下所有路径，按名字排序（等价 tar --sort=name）。"""
    names = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel == ".":
            names.append(".")
        else:
            names.append("./" + rel.replace(os.sep, "/"))
        for fn in filenames:
            p = os.path.relpath(os.path.join(dirpath, fn), root)
            names.append("./" + p.replace(os.sep, "/"))
    names.sort()
    return names


def make_tar_gz(root, epoch):
    """GNU tar（uid/gid=0、uname/gname 空、mtime=epoch、按名字排序）再 gzip。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for name in tar_members(root):
            full = root if name == "." else os.path.join(root, name[2:])
            st = os.lstat(full)
            ti = tarfile.TarInfo(name)
            ti.mode = stat.S_IMODE(st.st_mode)
            ti.uid = 0
            ti.gid = 0
            ti.uname = ""
            ti.gname = ""
            ti.mtime = epoch
            if stat.S_ISDIR(st.st_mode):
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            else:
                ti.type = tarfile.REGTYPE
                ti.size = st.st_size
                with open(full, "rb") as f:
                    tf.addfile(ti, f)
    return buf.getvalue()


def make_outer(epoch, members):
    """外层 tar：./debian-binary, ./data.tar.gz, ./control.tar.gz（顺序同 ipkg-build）。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for name, blob in members:
            ti = tarfile.TarInfo(name)
            ti.mode = 0o644
            ti.uid = 0
            ti.gid = 0
            ti.uname = ""
            ti.gname = ""
            ti.mtime = epoch
            ti.size = len(blob)
            tf.addfile(ti, io.BytesIO(blob))
    return buf.getvalue()


def pack(data_dir, control_dir, out_path, epoch):
    data_tar = make_tar_gz(data_dir, epoch)

    # Installed-Size = data.tar.gz 解压后的大小（OpenWrt 24.10 的算法；
    # 21.02/23.05 用的是压缩后大小，opkg 只当参考信息）
    ctrl_path = os.path.join(control_dir, "control")
    with open(ctrl_path, encoding="utf-8") as f:
        txt = f.read()
    txt = re.sub(r"(?m)^Installed-Size: .*$", "Installed-Size: %d" % len(data_tar), txt)
    with open(ctrl_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)

    control_tar = make_tar_gz(control_dir, epoch)
    outer = make_outer(epoch, (
        ("./debian-binary", b"2.0\n"),
        ("./data.tar.gz", gz(data_tar)),
        ("./control.tar.gz", gz(control_tar)),
    ))
    with open(out_path, "wb") as f:
        f.write(gz(outer))
    return out_path


def _tar_entries(blob):
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as tf:
        return [(m.name, stat.S_IMODE(m.mode), m.size, m.isdir())
                for m in tf.getmembers()]


def verify(path):
    raw = open(path, "rb").read()
    print("file      : %s (%d bytes)" % (os.path.basename(path), len(raw)))
    print("sha256    : %s" % hashlib.sha256(raw).hexdigest())
    if raw[:2] != b"\x1f\x8b":
        print("!! 外层不是 gzip（期望 1f 8b）: %r" % raw[:8])
        return 1
    outer = gzip.decompress(raw)
    print("gzip      : ok (解压后 %d 字节, 补零到 10240 的倍数: %s)"
          % (len(outer), len(outer) % 10240 == 0))
    with tarfile.open(fileobj=io.BytesIO(outer), mode="r:") as tf:
        members = tf.getmembers()
        names = [m.name for m in members]
        want = ["./debian-binary", "./data.tar.gz", "./control.tar.gz"]
        print("outer tar : %s" % names)
        if names != want:
            print("!! 外层成员/顺序不对，期望 %s" % want)
            return 1
        print("outer tar : 成员与顺序 ok (uid/gid=%s, uname=%r, mtime=%s)"
              % (members[0].uid, members[0].uname, members[0].mtime))
        deb = tf.extractfile(members[0]).read()
        if deb != b"2.0\n":
            print("!! debian-binary 内容不对: %r" % deb)
            return 1
        print("debian-bin: %r ok" % deb)
        data_gz = tf.extractfile(members[1]).read()
        ctrl_gz = tf.extractfile(members[2]).read()
    data_tar = gzip.decompress(data_gz)
    ctrl_tar = gzip.decompress(ctrl_gz)
    print("data.tar  : 解压后 %d 字节 -> Installed-Size" % len(data_tar))
    print("control   :")
    ctrl_names = []
    with tarfile.open(fileobj=io.BytesIO(ctrl_tar), mode="r:") as tf:
        for m in tf.getmembers():
            ctrl_names.append(m.name)
            if m.name == "./control":
                for line in tf.extractfile(m).read().decode("utf-8").splitlines():
                    print("            %s" % line)
    print("control   : 成员 %s" % ctrl_names)
    for req in ("./control", "./postinst", "./prerm"):
        if req not in ctrl_names:
            print("!! control.tar.gz 缺 %s" % req)
            return 1
    print("data      :")
    for name, mode, size, isdir in _tar_entries(data_tar):
        print("            %s %-8s %8d  %s" % (
            "d" if isdir else "-", oct(mode), size, name))
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "pack":
        data_dir, control_dir, out_path, epoch = sys.argv[2:6]
        pack(data_dir, control_dir, out_path, int(epoch))
        raw = open(out_path, "rb").read()
        print("    %s (%d 字节, sha256 %s)" % (out_path, len(raw),
                                               hashlib.sha256(raw).hexdigest()))
        return 0
    if cmd == "verify":
        return verify(sys.argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
PYEOF

echo "==> 3/4 打包"
mkdir -p "$REPO/ipk"
P_VER=$(mk_var "$PKG/Makefile" PKG_VERSION)
P_REL=$(mk_var "$PKG/Makefile" PKG_RELEASE)
P_OUT="$REPO/ipk/workbuddy2api_${P_VER}-${P_REL}_${ARCH}.ipk"
$PY "$PY_HELPER" pack "$DATA" "$CTRL" "$P_OUT" "$EPOCH"

echo "==> 4/4 自检"
$PY "$PY_HELPER" verify "$P_OUT"

echo "==> 完成（架构字段: $ARCH）"
