"""store.py — R2 bucket holding the collected documents.

Its own bucket, separate from anything else: this one is an archive of public
federal documents and is expected to grow to tens of gigabytes.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def load_env(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['CF_R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4", retries={"max_attempts": 8, "mode": "standard"}),
        region_name="auto",
    )


def bucket() -> str:
    return os.environ.get("CF_R2_BUCKET", "govdocs")


def ensure_bucket(s3) -> None:
    try:
        s3.head_bucket(Bucket=bucket())
    except ClientError:
        s3.create_bucket(Bucket=bucket())


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def exists(s3, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket(), Key=key)
        return True
    except ClientError:
        return False


def put(s3, key: str, data: bytes, content_type: str) -> None:
    s3.put_object(Bucket=bucket(), Key=key, Body=data, ContentType=content_type)
