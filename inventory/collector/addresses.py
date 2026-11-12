from dataclasses import dataclass


@dataclass(frozen=True)
class Address:
    role: str
    address: str
    ports: str | None = None
