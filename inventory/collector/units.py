from dataclasses import dataclass
from decimal import Decimal

_CPU = {"n": Decimal("0.000001"), "u": Decimal("0.001"), "m": Decimal(1)}
_BYTES = {
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60,
    "k": 10**3, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18,
}  # fmt: skip


def parse_cpu(value: str) -> int:
    """Kubernetes CPU quantity to millicores, rounded down."""
    value = str(value).strip()
    if value and value[-1] in _CPU:
        return int(Decimal(value[:-1]) * _CPU[value[-1]])
    return int(Decimal(value) * 1000)


def parse_bytes(value: str) -> int:
    """Kubernetes memory quantity to bytes, rounded down."""
    value = str(value).strip()
    for suffix in sorted(_BYTES, key=len, reverse=True):
        if value.endswith(suffix):
            return int(Decimal(value[: -len(suffix)]) * _BYTES[suffix])
    return int(Decimal(value))


@dataclass
class ClusterMetrics:
    nodes: int
    pods: int
    cpu_alloc_m: int
    cpu_req_m: int
    cpu_used_m: int | None
    mem_alloc_b: int
    mem_req_b: int
    mem_used_b: int | None


def metrics_from_cluster(cluster: dict, node_usage: list[dict] | None) -> ClusterMetrics:
    alloc = cluster["allocatable"]
    req = cluster["requested"]
    cpu_used = mem_used = None
    if node_usage is not None:
        cpu_used = sum(parse_cpu(n["usage"]["cpu"]) for n in node_usage)
        mem_used = sum(parse_bytes(n["usage"]["memory"]) for n in node_usage)
    return ClusterMetrics(
        nodes=cluster["nodeCount"],
        pods=int(req["pods"]),
        cpu_alloc_m=parse_cpu(alloc["cpu"]),
        cpu_req_m=parse_cpu(req["cpu"]),
        cpu_used_m=cpu_used,
        mem_alloc_b=parse_bytes(alloc["memory"]),
        mem_req_b=parse_bytes(req["memory"]),
        mem_used_b=mem_used,
    )
