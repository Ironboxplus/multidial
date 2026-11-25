# multidial

OpenWrt multi-uplink manager with macvlan DHCP uplinks, ePortal authentication,
mwan3 policies, per-uplink CAKE autorate and a LuCI dashboard.

The service ships disabled. Configure your own uplink device, probe URL, ping
targets and accounts before enabling it. Gateway, DNS, rebind exceptions and
sticky destination ranges are site-specific UCI settings. The example addresses
in this repository are synthetic and must be replaced for a real deployment.
Credentials belong in local UCI configuration.

The CI workflow tests Python, builds the dashboard and packages a noarch APK.
The NSS planner prints experimental tc batches for a dedicated test interface.
Hardware testing exposed a single-root-class requirement and subsequently lost
management connectivity during a virtual-bridge traffic experiment. The root
layout is corrected in the planner, but the connectivity failure is unresolved.
The NSS experiment is unsuitable for production deployment. The default CAKE
service does not execute these plans.

This publication starts a new history. Device screenshots and research captures
are excluded, and test fixtures use anonymized addresses and identifiers.
The package retains its existing Proprietary license declaration.
