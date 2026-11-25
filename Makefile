include $(TOPDIR)/rules.mk

PKG_NAME:=multidial
PKG_VERSION:=0.3.0
PKG_RELEASE:=4
PKG_LICENSE:=Proprietary

include $(INCLUDE_DIR)/package.mk

define Package/multidial
  SECTION:=net
  CATEGORY:=Network
  SUBMENU:=Routing and Redirection
  TITLE:=Campus multi-dial manager (macvlan + mwan3 + per-dial CAKE autorate)
  PKGARCH:=all
  DEPENDS:=+python3-light +python3-logging +python3-urllib +ip-full +curl \
	+kmod-macvlan +mwan3 +sqm-scripts +kmod-sched-cake +cake-autorate \
	+luci-compat +luci-lua-runtime
endef

define Package/multidial/description
Campus multi-dial manager: macvlan DHCP uplinks, portal authentication,
mwan3 members and a per-dial CAKE shaper floated by cake-autorate, with a
LuCI dashboard. Shipped dormant (globals.enabled=0, no accounts).
endef

define Package/multidial/conffiles
/etc/config/multidial
endef

define Build/Compile
endef

# web/dist is rebuilt from web/src by CI before packaging (see
# .github/workflows/ci.yml); the committed copy is only a fallback.
define Package/multidial/install
	$(INSTALL_DIR) $(1)/usr/lib/multidial/multidial
	$(CP) ./__init__.py ./__main__.py ./api.py ./config.py ./daemon.py ./dialer.py \
		./dns.py ./sqm.py ./nss.py ./nss_reconcile.py ./state.py ./sticky.py ./uci_sync.py $(1)/usr/lib/multidial/multidial/
	$(INSTALL_DIR) $(1)/usr/lib/multidial/multidial/tools
	$(INSTALL_DATA) ./tools/nss_ingress_plan.py ./tools/nss_leaf_check.py $(1)/usr/lib/multidial/multidial/tools/
	$(INSTALL_DATA) ./tools/nss-class-shaper.sh $(1)/usr/lib/multidial/multidial/tools/
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) ./multidial.init $(1)/etc/init.d/multidial
	$(INSTALL_DIR) $(1)/etc/hotplug.d/iface
	$(INSTALL_BIN) ./hotplug/17-multidial-sticky $(1)/etc/hotplug.d/iface/17-multidial-sticky
	$(INSTALL_DIR) $(1)/etc/config
	$(INSTALL_CONF) ./multidial.config $(1)/etc/config/multidial
	$(INSTALL_DIR) $(1)/usr/lib/lua/luci/controller
	$(INSTALL_DATA) ./luci/controller/multidial.lua $(1)/usr/lib/lua/luci/controller/multidial.lua
	$(INSTALL_DIR) $(1)/usr/lib/lua/luci/view/multidial
	$(INSTALL_DATA) ./luci/view/multidial/dashboard.htm $(1)/usr/lib/lua/luci/view/multidial/dashboard.htm
	$(INSTALL_DIR) $(1)/usr/share/rpcd/acl.d
	$(INSTALL_DATA) ./luci/acl/luci-app-multidial.json $(1)/usr/share/rpcd/acl.d/luci-app-multidial.json
	$(INSTALL_DIR) $(1)/www/luci-static/multidial
	$(INSTALL_DATA) ./web/dist/multidial.js ./web/dist/multidial.css $(1)/www/luci-static/multidial/
endef

$(eval $(call BuildPackage,multidial))
