"""Snowflake connection for /trigger (anyone else is welcome to reuse it).

Laptop: pass a connection name made with `snow connection add`.
GitHub Action: environment variables from repo secrets:
    SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER          required
    SNOWFLAKE_PRIVATE_KEY (+ _PASSPHRASE)      preferred: key-pair, see keygen.sh
    SNOWFLAKE_PASSWORD                         fallback
    SNOWFLAKE_ROLE                             optional
    SNOWFLAKE_WAREHOUSE / _DATABASE / _SCHEMA  default PR_GUARDIAN_WH / PR_GUARDIAN / CORE (sql/setup.sql)

Key-pair rather than password because Snowflake is phasing out password-only sign-in for
automated logins: https://docs.snowflake.com/en/user-guide/key-pair-auth
"""
from __future__ import annotations

import os

DEFAULTS = {"warehouse": "PR_GUARDIAN_WH", "database": "PR_GUARDIAN", "schema": "CORE"}


def configured(connection_name: str | None = None) -> bool:
    return bool(connection_name or os.environ.get("SNOWFLAKE_ACCOUNT"))


def connect(connection_name: str | None = None):
    import snowflake.connector

    if connection_name:
        # Reads ~/.snowflake/connections.toml or config.toml, the files `snow connection add` writes.
        return snowflake.connector.connect(connection_name=connection_name, **DEFAULTS)

    env = os.environ.get
    kw = {
        "account": env("SNOWFLAKE_ACCOUNT"),
        "user": env("SNOWFLAKE_USER"),
        "role": env("SNOWFLAKE_ROLE"),
        "warehouse": env("SNOWFLAKE_WAREHOUSE") or DEFAULTS["warehouse"],
        "database": env("SNOWFLAKE_DATABASE") or DEFAULTS["database"],
        "schema": env("SNOWFLAKE_SCHEMA") or DEFAULTS["schema"],
    }
    if not kw["account"] or not kw["user"]:
        raise RuntimeError("Set SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER (repo secrets).")

    pem = env("SNOWFLAKE_PRIVATE_KEY")
    if pem:
        from cryptography.hazmat.primitives import serialization

        pem = pem.strip().replace("\\n", "\n")  # tolerate a key pasted with literal \n
        passphrase = env("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
        key = serialization.load_pem_private_key(
            pem.encode(), password=passphrase.encode() if passphrase else None
        )
        kw["private_key"] = key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    elif env("SNOWFLAKE_PASSWORD"):
        kw["password"] = env("SNOWFLAKE_PASSWORD")
    else:
        raise RuntimeError("Set SNOWFLAKE_PRIVATE_KEY (preferred) or SNOWFLAKE_PASSWORD.")

    return snowflake.connector.connect(**{k: v for k, v in kw.items() if v})
