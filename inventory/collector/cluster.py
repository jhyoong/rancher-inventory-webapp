import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from inventory.collector.classify import build_profiles
from inventory.collector.kinds import KindSpec
from inventory.collector.optional import run_optional
from inventory.collector.platform import collect_api_versions
from inventory.collector.rules import Rules
from inventory.collector.transform import ClusterContext, Trimmed, skip_object, trim
from inventory.collector.units import metrics_from_cluster, parse_bytes, parse_cpu
from inventory.db.repo import (
    delete_stale,
    mark_cluster_failed,
    mark_cluster_ok,
    transaction,
    upsert_cluster,
    upsert_resources,
    write_metrics,
    write_api_versions,
    write_run_kind,
)
from inventory.rancher.client import RancherClient, RancherError

logger = logging.getLogger(__name__)

NODE_USAGE_PATH = "/apis/metrics.k8s.io/v1beta1/nodes"
POD_USAGE_PATH = "/apis/metrics.k8s.io/v1beta1/pods"


def pvc_users(pods: list[Trimmed]) -> dict[tuple[str, str], list[str]]:
    """(namespace, claim) -> sorted owner kinds of the pods that mount it ("Pod" for standalone pods)."""
    out: dict[tuple[str, str], set[str]] = {}
    for p in pods:
        for claim in p.data.get("pvc_claims") or []:
            out.setdefault((p.namespace, claim), set()).add(p.data.get("owner_kind") or "Pod")
    return {k: sorted(v) for k, v in out.items()}


def pod_usage_by_namespace(items: list[dict]) -> dict[str, tuple[int, int]]:
    """Sum pod metrics per namespace: (cpu millicores, memory bytes). Nothing is kept per pod."""
    out: dict[str, list[int]] = {}
    for pod in items:
        ns = (pod.get("metadata") or {}).get("namespace") or ""
        total = out.setdefault(ns, [0, 0])
        for c in pod.get("containers") or []:
            usage = (c or {}).get("usage") or {}
            try:
                total[0] += parse_cpu(usage.get("cpu") or "0")
                total[1] += parse_bytes(usage.get("memory") or "0")
            except (ArithmeticError, ValueError, TypeError):
                continue
    return {ns: (cpu, mem) for ns, (cpu, mem) in out.items()}


@dataclass
class ClusterResult:
    status: str  # "ok" or "failed"
    kinds_total: int
    kinds_done: int
    objects: int
    error: str | None
    kind_status: dict[str, str] = field(default_factory=dict)


async def collect_cluster(
    client: RancherClient,
    conn,
    site: str,
    cluster: dict,
    run_id: int,
    kinds: dict[str, KindSpec],
    rules: Rules | None,
    on_progress: Callable[[int, int], None],
    should_stop: Callable[[], bool],
    *,
    retention_days: int = 365,
) -> ClusterResult:
    """Collect every kind for one cluster.

    A kind that is forbidden (403) or not installed (404) is recorded and skipped.
    Any other error marks the kind failed, keeps its old rows, and fails the cluster
    after the remaining kinds have been tried. If should_stop() turns true, the
    remaining kinds are skipped and the result is failed with error "stopped".
    """
    cid = cluster["id"]
    total = len(kinds)
    upsert_cluster(conn, site, cluster, run_id)
    if cluster.get("state") != "active":
        error = f"cluster state: {cluster.get('state')}"
        mark_cluster_failed(conn, site, cid, error)
        return ClusterResult("failed", total, 0, 0, error)

    ctx = ClusterContext()
    in_use: Counter[tuple[str, str]] = Counter()
    kind_status: dict[str, str] = {}
    objects = 0
    done = 0
    failed_kinds: list[str] = []
    stopped = False
    for spec in kinds.values():
        if should_stop():
            stopped = True
            break
        status, error, count = "ok", None, 0
        try:
            raw = await client.list_kind(cid, spec.path)
        except RancherError as e:
            raw = None
            error = str(e)
            if e.status == 403:
                status = "forbidden"
            elif e.status == 404:
                status = "not_found"
            else:
                status = "failed"
                failed_kinds.append(spec.name)
        if raw is not None:
            items = [trim(spec, o, rules, ctx) for o in raw if not skip_object(spec.name, o)]
            if spec.name == "pods":
                ctx.pvc_users = pvc_users(items)
            in_use.update(t.in_use for t in items if t.in_use)  # counts objects per (apiVersion, kind)
            count = len(items)
            with transaction(conn):
                upsert_resources(conn, site, cid, run_id, items)
                delete_stale(conn, site, cid, spec.name, run_id)
                write_run_kind(conn, run_id, site, cid, spec.name, "ok", count, None)
            objects += count
        else:
            with transaction(conn):
                write_run_kind(conn, run_id, site, cid, spec.name, status, None, error)
        kind_status[spec.name] = status
        done += 1
        on_progress(done, total)

    try:  # metrics problems never fail a collected cluster
        try:
            usage = await client.list_kind(cid, NODE_USAGE_PATH)
        except RancherError:
            usage = None
        write_metrics(
            conn, site, cid, run_id, datetime.now(timezone.utc).date().isoformat(),
            metrics_from_cluster(cluster, usage), retention_days=retention_days,
        )  # fmt: skip
    except Exception:
        logger.exception("metrics skipped for %s/%s", site, cid)

    pod_usage = None
    if not stopped:
        try:
            pod_usage = pod_usage_by_namespace(await client.list_kind(cid, POD_USAGE_PATH))
        except RancherError as e:  # metrics-server missing or forbidden: usage stays empty
            logger.info("pod usage skipped for %s/%s: %s", site, cid, e)

    served = None
    if not stopped:
        try:
            served = await collect_api_versions(client, cid)
        except RancherError as e:
            logger.info("served API versions skipped for %s/%s: %s", site, cid, e)

    if rules is not None and not stopped and not failed_kinds:
        await run_optional(client, conn, site, cid, run_id, rules)

    error = None
    if not stopped and not failed_kinds:
        write_api_versions(conn, site, cid, served, in_use)
    if rules is not None and not stopped and not failed_kinds:
        try:
            build_profiles(conn, site, cid, run_id, rules, pod_usage=pod_usage)
        except Exception as e:  # collected data stays; the cluster is marked failed
            logger.exception("profile build failed for %s/%s", site, cid)
            error = f"profile build failed: {e}"
    if stopped:
        error = "stopped"
    elif failed_kinds:
        error = "kinds failed: " + ", ".join(failed_kinds)
    if error:
        mark_cluster_failed(conn, site, cid, error)
        return ClusterResult("failed", total, done, objects, error, kind_status)
    mark_cluster_ok(conn, site, cid, run_id, datetime.now(timezone.utc).isoformat())
    return ClusterResult("ok", total, done, objects, None, kind_status)
