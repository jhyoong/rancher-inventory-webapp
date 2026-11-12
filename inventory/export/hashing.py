"""Name hashing, the same formula as k8s-discovery.sh 1.0.0."""

import hashlib


def hash_name(salt: str, type_: str, value: str) -> str:
    """<type>-<first 10 hex of SHA-256(salt|type|value)>."""
    return f"{type_}-{hashlib.sha256(f'{salt}|{type_}|{value}'.encode()).hexdigest()[:10]}"


def salt_id(salt: str) -> str:
    """Short id of a salt, stored with the name map so maps of different salts never mix."""
    return hashlib.sha256(salt.encode()).hexdigest()[:8]
