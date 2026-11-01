#
# WorkBuddy2API-Hub · OpenWrt 版
#
# 纯 Python 包（只用标准库，零 pip 依赖），与 CPU 架构无关，
# 因此 PKGARCH:=all，产物是 workbuddy2api-1.6.20-r1.apk（架构 all）。
#
# 在 OpenWrt SDK 里构建：
#   sh wrt/openwrt/build-apk.sh <SDK 目录> [应用源码目录]
# 或手动：
#   cp -R wrt/openwrt/workbuddy2api <SDK>/package/
#   cd <SDK> && make package/workbuddy2api/compile V=s
#
# 上游源码（wb_*.py + dashboard.html）不在这里下载：build-apk.sh 会先把它
# 放进 files/usr/lib/workbuddy2api/，Build/Prepare 只做拷贝。
#
# PKG_VERSION 与 wb_proxy.py 的 server_version 必须一致：build-apk.sh /
# build-ipk.sh 会在打包前比对，release 工作流的 version job 也会比对，
# 对不上就拒绝出包（宁可不发，也不发一个版本号不对的包）。
#
include $(TOPDIR)/rules.mk

PKG_NAME:=workbuddy2api
PKG_VERSION:=1.10.0
PKG_RELEASE:=1

PKG_LICENSE:=MIT
PKG_MAINTAINER:=ardeyouxipianyi

PKG_BUILD_DIR:=$(BUILD_DIR)/$(PKG_NAME)-$(PKG_VERSION)

# 应用源码目录：优先包目录自带（整个包目录可独立拷进 SDK），
# 不存在时回退到 ../../app-src（仓库里原地构建时用）。
APP_SRC:=$(firstword $(wildcard $(CURDIR)/files/usr/lib/workbuddy2api) $(wildcard $(CURDIR)/../../app-src))
ifeq ($(APP_SRC),)
$(error 找不到应用源码（files/usr/lib/workbuddy2api 或 ../../app-src），请先运行 openwrt/build-apk.sh)
endif

include $(INCLUDE_DIR)/package.mk

define Package/workbuddy2api
  SECTION:=net
  CATEGORY:=Network
  TITLE:=WorkBuddy multi-account gateway with OpenAI-compatible API
  URL:=https://github.com/ardeyouxipianyi/workbuddy2api-hub
  DEPENDS:=+python3-light +python3-urllib +python3-uuid +python3-openssl +python3-sqlite3 +python3-codecs
  PKGARCH:=all
endef

define Package/workbuddy2api/description
  把腾讯 WorkBuddy（国际版 workbuddy.ai / 国内版 codebuddy.cn）包装成
  OpenAI 兼容 API 的多账号网关：/v1/chat/completions 与 /v1/responses，
  自带看板（8788 端口）做账号管理、用量统计、定价估算与每日自动任务。

  纯 Python 标准库实现（上游不引入任何 pip 依赖）。OpenWrt 把 ssl、sqlite3
  与完整编码表拆成了独立包，所以依赖里显式带上 python3-openssl /
  python3-sqlite3 / python3-codecs；Windows 专属功能（从桌面客户端内存
  回收解码密钥）在 Linux 上自动跳过，账号用看板的浏览器 OAuth 添加。

  安装后不会自动启动服务，启用方法：
    1) uci set workbuddy2api.main.panel_password='你的面板密码'
       uci commit workbuddy2api
    2) /etc/init.d/workbuddy2api enable && /etc/init.d/workbuddy2api start
    3) 浏览器打开 http://<路由器IP>:8788/ 登录，点「添加账号 (OAuth)」授权

  配置 /etc/config/workbuddy2api（含面板密码与可选固定 API Key，权限 600）；
  账号凭据与用量 /etc/workbuddy2api/（已加入 sysupgrade 保留列表）。
  升级只替换 /usr/lib/workbuddy2api 下的程序文件，配置与账号不受影响。

  升级走包管理器（opkg install / apk add），包内不含任何自更新逻辑。
endef

define Build/Prepare
	rm -rf $(PKG_BUILD_DIR)
	mkdir -p $(PKG_BUILD_DIR)
	$(CP) $(CURDIR)/files/. $(PKG_BUILD_DIR)/
endef

define Build/Compile
endef

define Package/workbuddy2api/install
	$(INSTALL_DIR) $(1)/usr/lib/workbuddy2api
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/usr/lib/workbuddy2api/wb_*.py $(1)/usr/lib/workbuddy2api/
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/usr/lib/workbuddy2api/dashboard.html $(1)/usr/lib/workbuddy2api/
	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/usr/bin/workbuddy2api-warm $(1)/usr/bin/workbuddy2api-warm
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/etc/init.d/workbuddy2api $(1)/etc/init.d/workbuddy2api
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) $(PKG_BUILD_DIR)/etc/config/workbuddy2api $(1)/etc/config/workbuddy2api
endef

# 配置文件（升级时按 conffile 处理：写进 /lib/apk/packages/workbuddy2api.conffiles）
define Package/workbuddy2api/conffiles
/etc/config/workbuddy2api
endef

# 安装后置脚本：准备数据目录、加入 sysupgrade 保留列表、收紧配置权限、
# 装面板缓存预热的 cron（workbuddy2api-warm，包内不含任何自更新逻辑）。
# 与 powerfee 同样的约定——首次安装不自动启用/启动服务
# （25.12 的 default_postinst 会先 enable+start，这里显式撤销），升级时不动现状。
define Package/workbuddy2api/postinst
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] || {
	mkdir -p /etc/workbuddy2api/accounts /etc/workbuddy2api/usage
	chmod 700 /etc/workbuddy2api /etc/workbuddy2api/accounts
	[ -f /etc/config/workbuddy2api ] && chmod 600 /etc/config/workbuddy2api
	grep -q '^/etc/workbuddy2api$$' /etc/sysupgrade.conf 2>/dev/null || \
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
	[ "$${PKG_UPGRADE}" = "1" ] || {
		/etc/init.d/workbuddy2api disable >/dev/null 2>&1
		/etc/init.d/workbuddy2api stop >/dev/null 2>&1
	}
}
endef

# 卸载后置脚本：清程序目录与预热 cron（python 运行留下的 __pycache__ 一并清掉）。
# /etc/workbuddy2api（账号/用量）与 /etc/config/workbuddy2api 是用户数据，保留。
define Package/workbuddy2api/postrm
#!/bin/sh
[ -n "$${IPKG_INSTROOT}" ] || {
	rm -rf /usr/lib/workbuddy2api
	rm -f /usr/bin/workbuddy2api-warm
	sed -i '/workbuddy2api-warm/d' /etc/crontabs/root 2>/dev/null
	/etc/init.d/cron restart >/dev/null 2>&1 || true
}
endef

$(eval $(call BuildPackage,workbuddy2api))
