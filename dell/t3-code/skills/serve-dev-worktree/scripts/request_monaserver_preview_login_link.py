#!/usr/bin/env python3
"""Request a fixture email login and return its captured one-time link."""

import argparse
import email
import email.policy
from email.utils import getaddresses
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def die(message):
    print(f"request_monaserver_preview_login_link: {message}", file=sys.stderr)
    raise SystemExit(1)


def clear_spool(spool):
    if spool.is_symlink():
        die("mail capture directory must not be a symlink")
    if not spool.exists():
        die("local preview mail capture is unavailable")
    for path in spool.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix == ".eml" or path.name.startswith("."):
            path.unlink(missing_ok=True)


def parse_message(path):
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return email.message_from_bytes(raw, policy=email.policy.default)


def message_text(message):
    for part in message.walk():
        if part.get_content_type() == "text/plain" and not part.is_attachment():
            try:
                return part.get_content()
            except (LookupError, UnicodeError):
                continue
    return ""


def extract_link(message, expected_host):
    for candidate in re.findall(r"https?://[^\s<>\"']+", message_text(message)):
        candidate = candidate.rstrip(".,;:!?)]}")
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.hostname != expected_host or not parsed.fragment.startswith("/email-login/callback?token="):
            continue
        if not parsed.fragment.partition("token=")[2]:
            continue
        return candidate
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--domain-suffix", default=os.environ.get("DEV_DOMAIN_SUFFIX", "dev.dell.lr-projects.de"))
    parser.add_argument("--username", default="stickitviewer")
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args()

    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?", args.slug):
        die("invalid preview slug")
    parsed_api = urllib.parse.urlsplit(args.api_url.rstrip("/"))
    expected_api = f"api-{args.slug}.{args.domain_suffix}"
    expected_web = f"web-{args.slug}.{args.domain_suffix}"
    if parsed_api.scheme not in ("http", "https") or parsed_api.netloc != expected_api or parsed_api.path or parsed_api.query or parsed_api.fragment:
        die("API URL does not match this preview slug")

    repo_root = Path(args.repo_root).resolve(strict=True)
    state_dir = repo_root / ".worktrees" / "preview" / args.slug
    spool = state_dir / "mail"
    if state_dir.is_symlink() or spool.is_symlink():
        die("preview mail capture path must not be a symlink")
    clear_spool(spool)

    link = None
    try:
        endpoint = args.api_url.rstrip("/") + "/api/v3/public/auth/email-link/request"
        payload = json.dumps({"email": args.username, "identifierType": "username"}).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 202:
                die("preview email sign-in request was not accepted")
            response.read()
        deadline = time.monotonic() + max(1, args.timeout)
        while time.monotonic() < deadline:
            messages = []
            for path in sorted(spool.glob("*.eml")):
                if path.is_symlink() or not path.is_file():
                    continue
                message = parse_message(path)
                if message is None or str(message.get("Subject", "")).strip().lower() != "sign in to stick-it":
                    continue
                recipients = {address.lower() for _, address in getaddresses(message.get_all("To", []))}
                if f"{args.username.lower()}@example.invalid" not in recipients:
                    continue
                messages.append(message)
            for message in messages:
                link = extract_link(message, expected_web)
                if link:
                    break
            if link:
                break
            time.sleep(0.5)
    except urllib.error.HTTPError as exc:
        die(f"preview email sign-in request failed with HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else "request timed out"
        die(f"could not reach the preview API: {reason}")
    finally:
        clear_spool(spool)

    if not link:
        die("no sign-in email reached the local capture service; check that the selected MonaServer adapter enables preview email capture and that the fixture is verified")
    print(link)


if __name__ == "__main__":
    main()
