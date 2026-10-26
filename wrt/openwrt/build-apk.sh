#!/bin/sh
# ============================================================================
# 一条命令构建 workbuddy2api 的 .apk 安装包
#
# 用法：
#   sh wrt/openwrt/build-apk.sh <SDK 目录> [应用源码目录]
#
#   <SDK 目录>      OpenWrt 25.12.x SDK（例：openwrt-sdk-25.12.2-x86-64_gcc-14.3.0_musl.Linux-x86_64）
#   [应用源码目录]  含 wb_*.py + dashboard.html 的目录（可选，默认本仓库检出）。
#
# 做的事：
#   1) 准备应用源码，校验版本号与 Makefile 的 PKG_VERSION 一致
#   2) 只挑 wb_*.py + dashboard.html + assets/ 同步进 wrt/openwrt/workbuddy2api/files/usr/lib/workbuddy2api/
#   3) 把 wrt/openwrt/workbuddy2api 拷进 SDK/package/
#   4) make package/workbuddy2api/compile（-j1，低配机器/容器上更稳）
#   5) 产物拷到 wrt/apk/workbuddy2api-<版本>-r<发布号>.apk 并打印 sha256
#
# 注意：构建机必须是 glibc 的 x86_64 Linux（SDK 自带宿主工具是 glibc 二进制，
# 不能在 musl 的 OpenWrt 上直接跑，也不能在 Windows 上跑）。CI 用 ubuntu-latest。
# ============================================================================
set -e

SDK="$1"
APP_SRC="$2"
[ -n "$SDK" ] || { echo "用法: $0 <OpenWrt SDK 目录> [应用源码目录]"; exit 1; }
[ -f "$SDK/rules.mk" ] || { echo "错误: $SDK 不像 OpenWrt SDK 目录（找不到 rules.mk）"; exit 1; }

# REPO 是 wrt/ 目录（本脚本住在 wrt/openwrt/ 下），仓库根在它上一层。
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$REPO/.." && pwd)"

# mk_var <makefile> <VAR> -> 变量值（取第一处赋值，与 build-ipk.sh 同一约定）
mk_var() {
	sed -n "s/^[[:space:]]*$2:=[[:space:]]*//p" "$1" | sed -n 1p
}

# ---- 1. 应用源码 ------------------------------------------------------------

if [ -z "$APP_SRC" ]; then
	APP_SRC="$ROOT"
	echo "==> 1/5 未指定应用源码目录，按仓库检出取: $APP_SRC"
fi

[ -f "$APP_SRC/wb_proxy.py" ] || {
	echo "错误: $APP_SRC 里没有 wb_proxy.py（应用源码目录应指向含 wb_*.py 的仓库根）" >&2; exit 1; }
[ -f "$APP_SRC/dashboard.html" ] || { echo "错误: $APP_SRC 里没有 dashboard.html" >&2; exit 1; }

DETECTED_VER="$(sed -n 's/.*server_version = "wb-proxy\/\([0-9.]*\)".*/\1/p' "$APP_SRC/wb_proxy.py" | head -n1)"
MAKE_VER="$(mk_var "$REPO/openwrt/workbuddy2api/Makefile" PKG_VERSION)"
PKG_REL="$(mk_var "$REPO/openwrt/workbuddy2api/Makefile" PKG_RELEASE)"
echo "    源码版本: ${DETECTED_VER:-未知}    Makefile: $MAKE_VER-r${PKG_REL:-?}"
[ -n "$DETECTED_VER" ] || { echo "错误: 从 $APP_SRC/wb_proxy.py 解析不出 server_version" >&2; exit 1; }
[ "$DETECTED_VER" = "$MAKE_VER" ] || {
	echo "错误: 源码版本（$DETECTED_VER）与 Makefile PKG_VERSION（${MAKE_VER:-未知}）不一致。" >&2
	echo "      升级时请把 wrt/openwrt/workbuddy2api/Makefile 的 PKG_VERSION 改成 $DETECTED_VER。" >&2
	exit 1
}

# ---- 2. 同步进包目录 --------------------------------------------------------

echo "==> 2/5 同步应用源码进包目录（只带 wb_*.py + dashboard.html + assets/）"
DST="$REPO/openwrt/workbuddy2api/files/usr/lib/workbuddy2api"
rm -rf "$DST"
mkdir -p "$DST"
for f in "$APP_SRC"/wb_*.py; do
	cp "$f" "$DST/"
done
cp "$APP_SRC/dashboard.html" "$DST/"
cp -R "$APP_SRC/assets" "$DST/assets"
ls "$DST" | wc -l | xargs echo "    文件数:"

# ---- 3. 拷进 SDK ------------------------------------------------------------

echo "==> 3/5 拷进 $SDK/package/"
rm -rf "$SDK/package/workbuddy2api"
cp -R "$REPO/openwrt/workbuddy2api" "$SDK/package/workbuddy2api"

# ---- 4. 编译 ----------------------------------------------------------------

echo "==> 4/5 编译"
cd "$SDK"
[ -f .config ] || make defconfig
make -j1 package/workbuddy2api/compile V=s

# ---- 5. 收集产物 ------------------------------------------------------------

echo "==> 5/5 收集产物到 $REPO/apk/"
mkdir -p "$REPO/apk"
OUT_APK="$(find "$SDK/bin" -name 'workbuddy2api-*.apk' | head -n1)"
[ -n "$OUT_APK" ] || { echo "错误: 没找到 workbuddy2api 的 .apk 产物"; exit 1; }
# 维护者约定的资产名（release 工作流直接按这个名字上传，不再改名）
OUT_NAME="workbuddy2api-${DETECTED_VER}-r${PKG_REL}.apk"
cp "$OUT_APK" "$REPO/apk/$OUT_NAME"
echo "    $OUT_APK"
echo "    -> apk/$OUT_NAME"

echo "==> 完成"
sha256sum "$REPO/apk/$OUT_NAME"
