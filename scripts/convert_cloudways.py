#!/usr/bin/env python3

import json
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "todd-clients-cloudways.txt"
OUTPUT_DIR = ROOT / "clients"


FIELD_MAP = {
    "client": "client_name",
    "email": "email",
    "website": "website_domain",
    "ip_address": "server_ip_address",
    "master_username": "master_username",
    "master_password": "master_password",
    "wp_cli_installed": "wp_cli_installed",
    "prod_public_html": "path_to_public_html",
    "path_to_public_html": "path_to_public_html",
    "staging": "is_staging",
    "woocommerce": "has_woocommerce",
}


def clean_value(value: str) -> str:
    value = value.strip()
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    value = re.sub(r"\*\*(.*?)\*\*", r"\1", value)
    return value.strip()


def parse_email(value: str) -> str:
    value = clean_value(value)
    if not value:
        return ""
    mailto = re.search(r"mailto:([^)\]]+)", value)
    if mailto:
        return mailto.group(1).strip()
    email = re.search(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", value, re.I)
    return email.group(1).strip() if email else value


def parse_website(value: str) -> str:
    value = clean_value(value)
    if not value:
        return ""
    link = re.search(r"\((https?://[^)\s]+)\)", value)
    if link:
        return link.group(1).strip()
    url = re.search(r"https?://[^\s)]+", value)
    if url:
        return url.group(0).strip()
    return value


def parse_bool(value: str) -> bool:
    value = clean_value(value).lower()
    return value in {"true", "yes", "1"}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "client"


def compatible(existing: dict, record: dict) -> bool:
    if existing.get("client_name", "") != record.get("client_name", ""):
        return False

    existing_ip = existing.get("server_ip_address", "")
    record_ip = record.get("server_ip_address", "")
    if existing_ip and record_ip and existing_ip != record_ip:
        return False

    existing_user = existing.get("master_username", "")
    record_user = record.get("master_username", "")
    if existing_user and record_user and existing_user != record_user:
        return False

    existing_pw = existing.get("master_password", "")
    record_pw = record.get("master_password", "")
    if existing_pw and record_pw and existing_pw != record_pw:
        return False

    # Require at least one concrete server-level overlap so distinct servers
    # for the same client name do not collapse together.
    return bool(
        (existing_ip and record_ip and existing_ip == record_ip)
        or (existing_user and record_user and existing_user == record_user)
        or (existing_pw and record_pw and existing_pw == record_pw)
    )


def merge_server_fields(existing: dict, record: dict) -> None:
    for key in ("email", "server_ip_address", "master_username", "master_password"):
        if not existing.get(key) and record.get(key):
            existing[key] = record[key]


def parse_records(text: str) -> list[dict]:
    text = text.replace("\r\n", "\n")
    records = []
    current = {}
    in_records = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# GLOBAL_ENVS"):
            continue
        if line.startswith("client:"):
            if current:
                records.append(current)
                current = {}
            in_records = True
        if not in_records:
            continue
        if line == "---" or line.startswith("# Application/Website"):
            continue

        match = re.match(r"([^:]+):\s*(.*)$", line)
        if not match:
            continue
        key, value = match.group(1).strip(), match.group(2)
        if key in {"application_username", "application_password", "ssh_key"}:
            continue
        mapped = FIELD_MAP.get(key)
        if not mapped:
            continue

        if mapped == "email":
            current[mapped] = parse_email(value)
        elif mapped == "website_domain":
            current[mapped] = parse_website(value)
        elif mapped in {"wp_cli_installed", "is_staging", "has_woocommerce"}:
            current[mapped] = parse_bool(value)
        else:
            current[mapped] = clean_value(value)

    if current:
        records.append(current)
    return records


def build_documents(records: list[dict]) -> dict[str, dict]:
    grouped = []
    for record in records:
        matched_group = None
        for group in grouped:
            if compatible(group["server"], record):
                matched_group = group
                break
        if matched_group is None:
            grouped.append(
                {
                    "server": {
                        "client_name": record.get("client_name", ""),
                        "email": record.get("email", ""),
                        "server_ip_address": record.get("server_ip_address", ""),
                        "master_username": record.get("master_username", ""),
                        "master_password": record.get("master_password", ""),
                    },
                    "applications": [record],
                }
            )
        else:
            merge_server_fields(matched_group["server"], record)
            matched_group["applications"].append(record)

    client_counts = defaultdict(int)
    documents = {}

    for group in grouped:
        server = group["server"]
        items = group["applications"]
        first = items[0]
        base_slug = slugify(server.get("client_name", "client"))
        client_counts[base_slug] += 1
        file_slug = base_slug if client_counts[base_slug] == 1 else f"{base_slug}-{client_counts[base_slug]}"

        applications = []
        for item in items:
            applications.append(
                {
                    "website_domain": item.get("website_domain", ""),
                    "path_to_public_html": item.get("path_to_public_html", ""),
                    "sftp_credentials": {
                        "username": "$SSH_USER",
                        "password": "$APP_PW",
                        "ssh_key": "$SSH_KEY",
                    },
                    "environment_flags": {
                        "wp_cli_installed": item.get("wp_cli_installed", False),
                        "is_staging": item.get("is_staging", False),
                        "has_woocommerce": item.get("has_woocommerce", False),
                    },
                }
            )

        documents[file_slug] = {
            "client_name": server.get("client_name", ""),
            "email": server.get("email", ""),
            "server_ip_address": server.get("server_ip_address", ""),
            "master_credentials": {
                "username": server.get("master_username", ""),
                "password": server.get("master_password", ""),
            },
            "applications": applications,
        }

    return documents


def main() -> None:
    records = parse_records(SOURCE.read_text())
    documents = build_documents(records)

    OUTPUT_DIR.mkdir(exist_ok=True)
    for path in OUTPUT_DIR.glob("*_cloudways.json"):
        path.unlink()

    for slug, document in documents.items():
        output_path = OUTPUT_DIR / f"{slug}_cloudways.json"
        output_path.write_text(json.dumps(document, indent=2) + "\n")

    print(f"Generated {len(documents)} files in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
