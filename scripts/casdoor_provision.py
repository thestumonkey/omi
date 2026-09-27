#!/usr/bin/env python3
"""
Casdoor provisioner — local replacement for uvx casdoor-provision / casdoor-app-delete.

Usage
-----
    # Provision orgs, apps, roles, groups from config/casdoor/*.yaml:
    python3 scripts/casdoor_provision.py [--env-file .env] [--config-dir ./config/casdoor]

    # Delete a Casdoor application:
    python3 scripts/casdoor_provision.py delete-app <name> [--env-file .env]

    # Dry run:
    python3 scripts/casdoor_provision.py --dry-run

Dependencies: httpx, pyyaml  (both in backend/pyproject.toml)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import httpx
import yaml

# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _load_env(env_file: Path) -> None:
    if not env_file.exists():
        print(f"  [warn] env file not found: {env_file} — relying on process env")
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Strip inline comments (space + # suffix), e.g. "postgres  # container name"
        value = value.split(" #")[0].rstrip()
        if key.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        print(f"ERROR: required env var {name} is not set", file=sys.stderr)
        sys.exit(1)
    return value


# ---------------------------------------------------------------------------
# Admin client (inline of ushadow_casdoor.client)
# ---------------------------------------------------------------------------

def _bootstrap_credentials(
    base_url: str,
    retries: int = 10,
    retry_delay: float = 3.0,
) -> tuple[str, str]:
    """Return (clientId, clientSecret) for app-built-in via HTTP login.

    Environment variables
    ---------------------
    CASDOOR_ADMIN_USER      Global admin username  (default: admin)
    CASDOOR_ADMIN_PASSWORD  Global admin password  (default: 123)
    """
    import time

    username = os.environ.get("CASDOOR_ADMIN_USER", "admin")
    password = os.environ.get("CASDOOR_ADMIN_PASSWORD", "123")

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with httpx.Client(timeout=10) as http:
                # Step 1 — authenticate; session cookie is stored in the client
                login_resp = http.post(
                    f"{base_url}/api/login",
                    json={
                        "username": username,
                        "password": password,
                        "application": "app-built-in",
                        "organization": "built-in",
                        "autoSignin": True,
                        "type": "login",
                    },
                )
                login_resp.raise_for_status()
                login_body = login_resp.json()
                if login_body.get("status") != "ok":
                    raise RuntimeError(f"Login failed: {login_body.get('msg') or login_body}")

                # Step 2 — session cookie is automatically sent by the shared client
                app_resp = http.get(
                    f"{base_url}/api/get-application",
                    params={"id": "admin/app-built-in"},
                )
            app_resp.raise_for_status()
            app_body = app_resp.json()
            app_data = app_body.get("data") or {}
            client_id = app_data.get("clientId", "")
            client_secret = app_data.get("clientSecret", "")
            if not client_id:
                raise RuntimeError(f"app-built-in returned no clientId: {app_body}")
            return client_id, client_secret

        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_err = exc
            if attempt < retries - 1:
                print(f"  [wait] Casdoor not reachable, retrying in {retry_delay:.0f}s… ({attempt + 1}/{retries})")
                time.sleep(retry_delay)
        except Exception as exc:
            raise RuntimeError(f"Credential bootstrap failed: {exc}") from exc

    raise RuntimeError(
        f"Could not reach Casdoor at {base_url} after {retries} attempts.\n"
        f"  Last error: {last_err}\n"
        f"  Check CASDOOR_ADMIN_USER / CASDOOR_ADMIN_PASSWORD and that Casdoor is running."
    ) from last_err


class CasdoorAdminClient:
    def __init__(self, base_url: str, org: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.org = org
        client_id, client_secret = _bootstrap_credentials(self.base_url)
        self._http = httpx.Client(
            params={"clientId": client_id, "clientSecret": client_secret},
            timeout=30,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "CasdoorAdminClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _check(self, resp: httpx.Response, action: str) -> dict:
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, dict) and body.get("status") not in ("ok", None):
            msg = body.get("msg") or ""
            if "duplicate key" in msg.lower():
                print(f"  [warn] {action}: already exists (duplicate key) — skipping")
                return body
            raise RuntimeError(f"Casdoor {action} failed: {msg or body}")
        return body

    def ensure(
        self,
        resource: str,
        resource_id: str,
        payload: dict,
        dry_run: bool = False,
        merge_on_update: bool = True,
        fetch_after_create: bool = False,
    ) -> dict | None:
        name = payload.get("name", resource_id)
        resp = self._http.get(f"{self.base_url}/api/get-{resource}", params={"id": resource_id})
        resp.raise_for_status()
        existing = resp.json().get("data") if isinstance(resp.json(), dict) else None

        if existing:
            print(f"↩  {resource.capitalize()} '{name}' exists — updating")
            if not dry_run:
                merged = {**existing, **payload} if merge_on_update else payload
                r = self._http.post(f"{self.base_url}/api/update-{resource}",
                                    params={"id": resource_id}, json=merged)
                self._check(r, f"update-{resource} {name}")
            return existing

        print(f"→ {resource.capitalize()} '{name}' not found — creating")
        if dry_run:
            return None
        r = self._http.post(f"{self.base_url}/api/add-{resource}", json=payload)
        self._check(r, f"add-{resource} {name}")

        if fetch_after_create:
            r2 = self._http.get(f"{self.base_url}/api/get-{resource}", params={"id": resource_id})
            created = self._check(r2, f"get-{resource} {name}").get("data") or {}
            print(f"✓ {resource.capitalize()} '{name}' created")
            return created

        print(f"✓ {resource.capitalize()} '{name}' created")
        return payload

    def delete(self, resource: str, resource_id: str, payload: dict, dry_run: bool = False) -> None:
        name = payload.get("name", resource_id)
        resp = self._http.get(f"{self.base_url}/api/get-{resource}", params={"id": resource_id})
        resp.raise_for_status()
        existing = resp.json().get("data") if isinstance(resp.json(), dict) else None

        if not existing:
            print(f"  [skip] {resource.capitalize()} '{name}' not found — nothing to delete")
            return

        print(f"✗ Deleting {resource} '{name}'")
        if not dry_run:
            r = self._http.post(f"{self.base_url}/api/delete-{resource}", json={**existing, **payload})
            self._check(r, f"delete-{resource} {name}")
            print(f"✓ {resource.capitalize()} '{name}' deleted")

    def write_credentials(self, env_file: Path, client_id: str, client_secret: str) -> None:
        if not env_file.exists():
            print(f"  [warn] {env_file} not found — skipping credential write-back")
            return
        text = env_file.read_text()

        def replace_or_append(content: str, key: str, value: str) -> tuple[str, bool]:
            pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
            if pattern.search(content):
                return pattern.sub(f"{key}={value}", content), True
            return content + f"\n{key}={value}\n", False

        text, found_id = replace_or_append(text, "CASDOOR_CLIENT_ID", client_id)
        text, found_secret = replace_or_append(text, "CASDOOR_CLIENT_SECRET", client_secret)
        env_file.write_text(text)
        print(f"✓ {'updated' if found_id else 'appended'} CASDOOR_CLIENT_ID in {env_file}")
        print(f"✓ {'updated' if found_secret else 'appended'} CASDOOR_CLIENT_SECRET in {env_file}")


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------

def _load_yaml(filename: str, config_dir: Path) -> dict:
    path = config_dir / filename
    if not path.exists():
        return {}
    text = re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), m.group(0)),
                  path.read_text())
    return yaml.safe_load(text) or {}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_provision(args: argparse.Namespace) -> None:
    env_path = Path(args.env_file)
    _load_env(env_path)

    config_dir = Path(args.config_dir)
    port = os.environ.get("CASDOOR_PORT", "8082")
    base_url = (
        os.environ.get("CASDOOR_EXTERNAL_URL")
        or os.environ.get("CASDOOR_ENDPOINT")
        or f"http://localhost:{port}"
    ).rstrip("/")
    app_name = os.environ.get("CASDOOR_APP_NAME", "")

    orgs_config = _load_yaml("organizations.yaml", config_dir)
    admin_org = orgs_config.get("admin_org", "admin")
    user_org = orgs_config.get("user_org", "built-in")

    if args.dry_run:
        print("[DRY-RUN MODE] No changes will be made.\n")

    with CasdoorAdminClient(base_url, admin_org) as admin:

        # Patch built-in org
        print("\n── Built-in org patch ─────────────────────────────────────────────────")
        admin.ensure("organization", "admin/built-in",
                     {"owner": "admin", "name": "built-in", "defaultApplication": ""},
                     dry_run=args.dry_run, merge_on_update=True)

        print("\n── Organizations ──────────────────────────────────────────────────────")
        for org_def in orgs_config.get("organizations", []):
            admin.ensure("organization", f"{admin_org}/{org_def['name']}",
                         {"owner": admin_org, **org_def},
                         dry_run=args.dry_run, merge_on_update=True)

        print("\n── Providers ──────────────────────────────────────────────────────────")
        for provider_def in _load_yaml("providers.yaml", config_dir).get("providers", []):
            admin.ensure("provider", f"{admin_org}/{provider_def['name']}",
                         {"owner": admin_org, **provider_def},
                         dry_run=args.dry_run, merge_on_update=True)

        print("\n── Applications ───────────────────────────────────────────────────────")
        apps_config = _load_yaml("apps.yaml", config_dir)
        app_credentials: dict[str, tuple[str, str]] = {}
        for app_def in apps_config.get("apps", []):
            result = admin.ensure("application", f"{admin_org}/{app_def['name']}",
                                  {"owner": admin_org, "organization": user_org, **app_def},
                                  dry_run=args.dry_run, merge_on_update=True, fetch_after_create=True)
            if result and (cid := result.get("clientId")):
                app_credentials[app_def["name"]] = (cid, result.get("clientSecret", ""))

        print("\n── Groups ─────────────────────────────────────────────────────────────")
        for group_def in _load_yaml("groups.yaml", config_dir).get("groups", []):
            admin.ensure("group", f"{user_org}/{group_def['name']}",
                         {"owner": user_org, **group_def},
                         dry_run=args.dry_run, merge_on_update=False)

        print("\n── Roles ──────────────────────────────────────────────────────────────")
        for role_def in _load_yaml("roles.yaml", config_dir).get("roles", []):
            admin.ensure("role", f"{user_org}/{role_def['name']}",
                         {"owner": user_org, "users": [], "roles": [], "domains": [],
                          "isEnabled": True, **role_def},
                         dry_run=args.dry_run, merge_on_update=False)

        # App admin user
        raw_user = os.environ.get("CASDOOR_APP_ADMIN_USER", "admin")
        username = raw_user.split("/")[-1]
        password = str(os.environ.get("CASDOOR_APP_ADMIN_PASSWORD", "") or app_name)
        print("\n── App admin user ─────────────────────────────────────────────────────")
        print(f"  user: {user_org}/{username}")
        admin.ensure("user", f"{user_org}/{username}",
                     {"owner": user_org, "name": username, "displayName": "Admin",
                      "password": password, "type": "normal-user",
                      "signupApplication": app_name, "isAdmin": True,
                      "isForbidden": False, "isDeleted": False},
                     dry_run=args.dry_run, merge_on_update=False)

    if not args.dry_run and app_name and app_name in app_credentials:
        cid, csecret = app_credentials[app_name]
        print("\n── Application credentials ────────────────────────────────────────────")
        with CasdoorAdminClient(base_url, admin_org) as admin:
            admin.write_credentials(env_path, cid, csecret)

    print("\n✓ Casdoor provisioning complete")


def cmd_delete_app(args: argparse.Namespace) -> None:
    env_path = Path(args.env_file)
    _load_env(env_path)

    port = os.environ.get("CASDOOR_PORT", "8082")
    base_url = (
        os.environ.get("CASDOOR_EXTERNAL_URL")
        or os.environ.get("CASDOOR_ENDPOINT")
        or f"http://localhost:{port}"
    ).rstrip("/")
    orgs_config = _load_yaml("organizations.yaml", Path(args.config_dir)) if Path(args.config_dir).exists() else {}
    admin_org = orgs_config.get("admin_org", "admin")

    with CasdoorAdminClient(base_url, admin_org) as admin:
        admin.delete("application", f"{admin_org}/{args.app_name}",
                     {"owner": admin_org, "name": args.app_name},
                     dry_run=args.dry_run)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Casdoor provisioner")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--config-dir", default="./config/casdoor")
    parser.add_argument("--dry-run", action="store_true")

    sub = parser.add_subparsers(dest="command")

    # delete-app subcommand
    del_parser = sub.add_parser("delete-app", help="Delete a Casdoor application")
    del_parser.add_argument("app_name", help="Application name to delete")
    del_parser.add_argument("--env-file", default=".env")
    del_parser.add_argument("--config-dir", default="./config/casdoor")
    del_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.command == "delete-app":
        cmd_delete_app(args)
    else:
        cmd_provision(args)


if __name__ == "__main__":
    main()
