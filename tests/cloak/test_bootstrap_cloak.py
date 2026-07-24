"""Static contracts for the one-command Cloak deployment path."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_bootstraps_enable_strict_manager_owned_cdp_path() -> None:
    linux_bootstrap = _read("scripts/bootstrap_cloak.sh")
    windows_bootstrap = _read("scripts/bootstrap_cloak.ps1")
    linux_installer = _read("scripts/install_cloak.sh")
    windows_installer = _read("scripts/install_cloak.ps1")
    core_windows_installer = _read("scripts/install.ps1")

    assert "--non-interactive --skip-setup --skip-browser" in linux_bootstrap
    assert "--configure-provider --strict" in linux_bootstrap
    assert 'export PATH="$HERMES_DATA_DIR/bin:$HERMES_DATA_DIR/node/bin:$PATH"' in linux_bootstrap
    assert "-NonInteractive -SkipSetup -SkipBrowser" in windows_bootstrap
    assert "-ConfigureProvider -Strict" in windows_bootstrap

    assert 'HERMES_DATA_DIR/bin/uv' in linux_installer
    assert "PY_INSTALL_UV" in linux_installer
    assert "browser.cloud_provider cloak" in linux_installer
    assert "agent_browser_healthy" in linux_installer
    assert "managedUv = Join-Path $HermesHome" in windows_installer
    assert "Test-AgentBrowser" in windows_installer
    assert "browser.cloud_provider cloak" in windows_installer

    assert "playwright install chromium" not in linux_installer
    assert "playwright install chromium" not in windows_installer
    assert "[switch]$SkipBrowser" in core_windows_installer
    assert "if ($browserNpmOk -and -not $SkipBrowser)" in core_windows_installer


def test_windows_bootstrap_offers_an_isolated_full_compose_mode() -> None:
    windows_bootstrap = _read("scripts/bootstrap_cloak.ps1")
    compose = _read("docker-compose.cloak.yml")
    dockerfile = _read("Dockerfile")

    assert 'ValidateSet("auto", "native", "compose")' in windows_bootstrap
    assert windows_bootstrap.isascii()
    assert 'else { "hermes-cloak-edition" }' in windows_bootstrap
    assert "docker-compose.cloak.yml" in windows_bootstrap
    assert "CLOAK_AUTH_TOKEN=$(New-CloakToken)" in windows_bootstrap
    assert "WriteAllLines($composeEnvFile" in windows_bootstrap
    assert 'Invoke-DockerCompose $docker $baseArguments @("build", "hermes")' in windows_bootstrap
    assert 'Invoke-DockerCompose $docker $baseArguments @("up", "-d", "--remove-orphans")' in windows_bootstrap
    assert "bridge_readiness.py" in windows_bootstrap
    assert "StatusCode -eq 401" in windows_bootstrap
    assert "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=$(New-CloakToken)" in windows_bootstrap

    assert "manager:" in compose
    assert "bridge:" in compose
    assert "setup:" in compose
    assert "hermes:" in compose
    assert "\n  dashboard:\n" not in compose
    assert 'HERMES_GATEWAY_BOOTSTRAP_STATE: running' in compose
    assert 'HERMES_DASHBOARD: "1"' in compose
    assert 'command: ["sleep", "infinity"]' in compose
    assert "CLOAK_CDP_PROXY_BASE: http://bridge:8081" in compose
    assert "cloak-profiles:/data" in compose
    assert "hermes-data:/opt/data" in compose
    assert '"127.0.0.1:${CLOAK_MANAGER_PORT:-8180}:8080"' in compose
    assert '"127.0.0.1:${HERMES_DASHBOARD_PORT:-9119}:9119"' in compose
    assert "HERMES_DASHBOARD_BASIC_AUTH_USERNAME" in compose
    assert "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" in compose

    assert '"cloakbrowser>=0.3"' in dockerfile
    assert '"pydoll-python>=2.20"' in dockerfile

def test_removed_registration_skills_are_not_referenced_by_runtime_docs() -> None:
    documents = (
        "README.md",
        "scripts/cloak/_smoke_check.py",
        "skills/cloak-proxy-pool/SKILL.md",
    )
    text = "\n".join(_read(path) for path in documents)
    removed = "cloak" + "-account-registration"
    assert removed not in text
    assert removed + "-pro" not in text
