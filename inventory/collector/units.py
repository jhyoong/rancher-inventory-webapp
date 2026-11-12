from collections.abc import Callable
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
    """Kubernetes memory quantity to bytes, rounded down. "m" is milli (1/1000 byte)."""
    value = str(value).strip()
    if value.endswith("m"):
        return int(Decimal(value[:-1]) / 1000)
    for suffix in sorted(_BYTES, key=len, reverse=True):
        if value.endswith(suffix):
            return int(Decimal(value[: -len(suffix)]) * _BYTES[suffix])
    return int(Decimal(value))


@dataclass
class ClusterMetrics:
    nodes: int | None
    pods: int | None
    cpu_alloc_m: int | None
    cpu_req_m: int | None
    cpu_used_m: int | None
    mem_alloc_b: int | None
    mem_req_b: int | None
    mem_used_b: int | None


def _opt(parse: Callable[[str], int], value) -> int | None:
    return None if value is None else parse(value)


def _sum_usage(parse: Callable[[str], int], node_usage: list[dict] | None, key: str) -> int | None:
    """Sum one usage value over nodes; None when any node lacks it."""
    if node_usage is None:
        return None
    values = [(n.get("usage") or {}).get(key) for n in node_usage]
    if any(v is None for v in values):
        return None
    return sum(parse(v) for v in values)


def metrics_from_cluster(cluster: dict, node_usage: list[dict] | None) -> ClusterMetrics:
    """Metrics from a /v3 cluster item. Missing values become None."""
    alloc = cluster.get("allocatable") or {}
    req = cluster.get("requested") or {}
    return ClusterMetrics(
        nodes=cluster.get("nodeCount"),
        pods=_opt(int, req.get("pods")),
        cpu_alloc_m=_opt(parse_cpu, alloc.get("cpu")),
        cpu_req_m=_opt(parse_cpu, req.get("cpu")),
        cpu_used_m=_sum_usage(parse_cpu, node_usage, "cpu"),
        mem_alloc_b=_opt(parse_bytes, alloc.get("memory")),
        mem_req_b=_opt(parse_bytes, req.get("memory")),
        mem_used_b=_sum_usage(parse_bytes, node_usage, "memory"),
    )
