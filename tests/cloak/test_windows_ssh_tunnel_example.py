from pathlib import Path


ROOT = Path("scripts/examples/windows-ssh-tunnel")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_windows_ssh_tunnel_example_is_safe_and_complete():
    expected = {
        "README.md",
        "config.bat.example",
        "open_everything.bat",
        "open_manager.bat",
        "repair_key_permissions.bat",
        "repair_key_permissions.ps1",
        "ssh_console.bat",
        "test_connection.bat",
    }
    assert {path.name for path in ROOT.iterdir() if path.is_file()} == expected

    config = _read("config.bat.example")
    all_tunnels = _read("open_everything.bat")
    repair = _read("repair_key_permissions.ps1")
    assert "YOUR_SERVER_IP" in config
    assert "C:\\path\\to\\id_ed25519" in config
    assert "ExitOnForwardFailure=yes" in all_tunnels
    assert "-L %LOCAL_DASHBOARD_PORT%:127.0.0.1:9119" in all_tunnels
    assert "-L %LOCAL_CLOAK_MANAGER_PORT%:127.0.0.1:8080" in all_tunnels
    assert "-L %LOCAL_CDP_PROXY_PORT%:127.0.0.1:8081" in all_tunnels
    assert "SetAccessRuleProtection($true, $false)" in repair
    assert "FileSystemRights]::Read" in repair