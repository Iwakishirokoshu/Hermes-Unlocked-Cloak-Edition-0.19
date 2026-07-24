import sys
from pathlib import Path

sys.path.insert(0, ".")
from plugins.browser.cloak.paths import cloak_dir
from plugins.browser.cloak import session_leases as sl
from plugins.browser.cloak.provider import CloakBrowserProvider
from plugins.browser.cloak.proxy_format import normalize_proxy

print("paths", cloak_dir())
sl.put(sl.Lease("a", "pa", "ws://1"))
sl.put(sl.Lease("b", "pb", "ws://2"))
assert sl.get("a").profile_id == "pa" and sl.get("b").profile_id == "pb"
print("leases ok")
print("provider", CloakBrowserProvider().get_setup_schema()["post_setup"])
print("proxy", normalize_proxy("socks5://u:p@1.2.3.4:1080"))
m = Path("scripts/cloak/nginx/cloak-upgrade-map.conf").read_text(encoding="utf-8")
t = Path("scripts/cloak/nginx/cloak-cdp-proxy.conf.template").read_text(encoding="utf-8")
assert "cloak_connection_upgrade" in m and "cloak_connection_upgrade" in t
assert "Connection $http_connection" not in t
print("nginx ok")
dash = Path("hermes_cli/cloak_dashboard.py").read_text(encoding="utf-8")
assert 'id="reveal"' not in dash
assert "Raw token is never included" in dash
print("dashboard ok")
bt = Path("tools/browser_tool.py").read_text(encoding="utf-8")
assert "_explicit_cloud_provider_key" in bt and "Fail-closed" in bt
print("fail-closed ok")
assert Path("scripts/install_cloak.ps1").exists()
assert Path("scripts/cloak/cdp_bridge.py").exists()
assert Path("skills/cloak-proxy-pool/SKILL.md").exists()
print("ALL GOOD")
