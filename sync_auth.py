"""
Auth Sync Script for NotebookLM Service

Copies your local NotebookLM authentication cookies to a remote VPS.
Run this locally whenever the service reports auth expired (every 2-4 weeks).

Usage:
    python sync_auth.py                    # Sync to default VPS
    python sync_auth.py --host 1.2.3.4     # Sync to specific host
    python sync_auth.py --check            # Just check if local auth is valid
    python sync_auth.py --relogin          # Re-authenticate first, then sync

Prerequisites:
    - notebooklm-py installed locally (pip install notebooklm-py)
    - SSH access to the VPS (key-based auth recommended)
    - Docker running on the VPS with the nlm-service container
"""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Default VPS connection
DEFAULT_HOST = os.environ.get("NLM_VPS_HOST", "100.77.129.29")
DEFAULT_USER = os.environ.get("NLM_VPS_USER", "mr-prime")
DEFAULT_CONTAINER = "nlm-service"

# Local auth file location
LOCAL_AUTH = Path.home() / ".notebooklm" / "storage_state.json"


def check_local_auth() -> bool:
    """Verify local NotebookLM auth is valid."""
    try:
        result = asyncio.run(_check_auth())
        return result
    except Exception as e:
        print(f"Auth check failed: {e}")
        return False


async def _check_auth():
    from notebooklm.client import NotebookLMClient
    async with await NotebookLMClient.from_storage() as client:
        notebooks = await client.notebooks.list()
        print(f"Local auth valid. {len(notebooks)} notebooks found.")
        return True


def relogin():
    """Run the notebooklm login flow."""
    print("Opening browser for Google authentication...")
    print("Log in to your Google account in the browser window that opens.")
    subprocess.run([sys.executable, "-m", "notebooklm.notebooklm_cli", "login"], check=True)
    print("Login complete.")


def sync_to_vps(host: str, user: str, container: str):
    """Copy local auth file to VPS Docker container."""
    if not LOCAL_AUTH.exists():
        print(f"ERROR: Local auth file not found at {LOCAL_AUTH}")
        print("Run: python sync_auth.py --relogin")
        sys.exit(1)

    print(f"Syncing auth to {user}@{host}...")

    # SCP to VPS temp location
    tmp_path = "/tmp/nlm_storage_state.json"
    scp_cmd = ["scp", str(LOCAL_AUTH), f"{user}@{host}:{tmp_path}"]
    result = subprocess.run(scp_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"SCP failed: {result.stderr}")
        sys.exit(1)

    # Copy into Docker volume
    ssh_cmd = [
        "ssh", f"{user}@{host}",
        f"docker cp {tmp_path} {container}:/data/storage_state.json && rm {tmp_path} && echo OK"
    ]
    result = subprocess.run(ssh_cmd, capture_output=True, text=True)
    if result.returncode != 0 or "OK" not in result.stdout:
        print(f"Docker copy failed: {result.stderr}")
        sys.exit(1)

    print("Auth synced successfully.")

    # Verify by hitting the health endpoint
    verify_cmd = [
        "ssh", f"{user}@{host}",
        f"docker exec {container} python -c \"import asyncio; from notebooklm.client import NotebookLMClient; asyncio.run((lambda: NotebookLMClient.from_storage())())\" 2>&1 | tail -1"
    ]
    subprocess.run(verify_cmd)
    print("Done. The NLM service should now be authenticated.")


def main():
    parser = argparse.ArgumentParser(description="Sync NotebookLM auth to VPS")
    parser.add_argument("--host", default=DEFAULT_HOST, help="VPS hostname or IP")
    parser.add_argument("--user", default=DEFAULT_USER, help="SSH user")
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help="Docker container name")
    parser.add_argument("--check", action="store_true", help="Only check if local auth is valid")
    parser.add_argument("--relogin", action="store_true", help="Re-authenticate before syncing")
    args = parser.parse_args()

    if args.check:
        valid = check_local_auth()
        sys.exit(0 if valid else 1)

    if args.relogin:
        relogin()

    if not check_local_auth():
        print("Local auth is invalid or expired.")
        print("Run: python sync_auth.py --relogin")
        sys.exit(1)

    sync_to_vps(args.host, args.user, args.container)


if __name__ == "__main__":
    main()
