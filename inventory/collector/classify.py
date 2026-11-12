import json
from collections import defaultdict
from dataclasses import dataclass
from fnmatch import fnmatchcase

from inventory.collector.rules import Rules
from inventory.collector.units import parse_bytes
from inventory.db.repo import transaction

WORKLOAD_KINDS = ("deployments", "statefulsets", "daemonsets")


@dataclass
class Flags:
    has_workloads: bool
    stateful: bool
    helm: bool
    custom_crds: bool
    hostpath: bool


def _matches(value: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(value, p) for p in patterns)


def is_system_namespace(ns: str, rules: Rules) -> bool:
    return _matches(ns, rules.system_namespaces)


def crd_group_matches(group: str, entry: str) -> bool:
    """True when the group fnmatches the entry, equals it, or is a subdomain of it."""
    return group == entry or group.endswith("." + entry) or fnmatchcase(group, entry)


def is_system_crd_group(group: str, rules: Rules) -> bool:
    return any(crd_group_matches(group, e) for e in rules.system_crd_groups)


def class_label(f: Flags) -> str:
    if not f.has_workloads:
        return "empty"
    if not f.stateful:
        return "stateless"
    label = "stateful"
    if f.helm:
        label += "-helm"
    if f.custom_crds:
        label += "-crd"
    return label


def _bytes(value) -> int | None:
    try:
        return parse_bytes(value)
    except (ArithmeticError, ValueError, TypeError):
        return None


def build_profiles(
    conn, site: str, cluster_id: str, run_id: int, rules: Rules,
    pod_usage: dict[str, tuple[int, int]] | None = None,
) -> Flags:  # fmt: skip
    """Rebuild namespace_profile rows and the cluster_profile row from stored resources.

    Flags and the statefulset, PVC and Helm counts in cluster_profile come from user
    namespaces only. lb_services and ingresses count all namespaces.
    """
    where = "WHERE site=? AND cluster_id=?"
    args = (site, cluster_id)

    def rows(sql: str, *extra):
        return conn.execute(sql, (*args, *extra)).fetchall()

    ns = defaultdict(lambda: defaultdict(int))
    stateful_ns: set[str] = set()
    names: set[str] = set()

    for r in rows(f"SELECT name FROM resources {where} AND kind='namespaces'"):
        names.add(r["name"])

    helm_labeled = set()  # namespaces with a stateful workload managed by Helm
    for r in rows(
        f"""SELECT r.kind, r.namespace,
                   json_extract(r.data, '$.hostpath') AS hostpath,
                   json_extract(r.data, '$.pvc_claims') AS claims,
                   EXISTS (SELECT 1 FROM resource_labels l WHERE l.resource_id = r.id
                           AND l.key='app.kubernetes.io/managed-by' AND l.value='Helm') AS helm
            FROM resources r {where} AND r.kind IN ('deployments','statefulsets','daemonsets')"""
    ):
        n = r["namespace"]
        names.add(n)
        ns[n]["workloads"] += 1
        if r["kind"] == "statefulsets":
            ns[n]["statefulsets"] += 1
        if r["hostpath"]:
            ns[n]["hostpath_workloads"] += 1
        has_state = r["kind"] == "statefulsets" or json.loads(r["claims"] or "[]") != []
        if has_state and r["helm"]:
            helm_labeled.add(n)

    for r in rows(
        f"""SELECT namespace,
                   coalesce(json_extract(data, '$.status.capacity.storage'),
                            json_extract(data, '$.spec.resources.requests.storage')) AS cap
            FROM resources {where} AND kind='persistentvolumeclaims'
              AND json_extract(data, '$.status.phase')='Bound'"""
    ):
        n = r["namespace"]
        names.add(n)
        ns[n]["pvc_bound"] += 1
        size = _bytes(r["cap"]) if r["cap"] else None
        if size is not None:
            ns[n]["pvc_bytes"] += size

    for r in rows(
        f"""SELECT namespace FROM resources {where} AND kind='helmreleases'
              AND json_extract(data, '$.spec.info.status')='deployed'"""
    ):
        names.add(r["namespace"])
        ns[r["namespace"]]["helm_releases"] += 1

    for r in rows(
        f"SELECT namespace, json_extract(data, '$.spec.type') AS t FROM resources {where} AND kind='services'"
    ):
        names.add(r["namespace"])
        if r["t"] == "LoadBalancer":
            ns[r["namespace"]]["lb_services"] += 1
    for r in rows(f"SELECT namespace FROM resources {where} AND kind='ingresses'"):
        names.add(r["namespace"])
        ns[r["namespace"]]["ingresses"] += 1

    # a namespace object counts as part of its own namespace
    for r in rows(
        f"""SELECT CASE WHEN r.kind='namespaces' THEN r.name ELSE r.namespace END AS n, count(*) AS c
            FROM resources r JOIN resource_annotations a ON a.resource_id = r.id
            WHERE r.site=? AND r.cluster_id=? AND a.custom=1
            GROUP BY n"""
    ):
        if r["n"]:
            names.add(r["n"])
            ns[r["n"]]["custom_annotations"] += r["c"]

    names.discard("")
    for n in names:
        p = ns[n]
        if not is_system_namespace(n, rules) and (p["statefulsets"] or p["pvc_bound"]):
            stateful_ns.add(n)

    user = [n for n in names if not is_system_namespace(n, rules)]
    local_pv = rows(f"SELECT 1 FROM resources {where} AND kind='persistentvolumes' AND json_extract(data, '$.local') LIMIT 1")

    groups = sorted(
        {
            r["g"]
            for r in rows(
                f"""SELECT DISTINCT json_extract(data, '$.spec.group') AS g
                    FROM resources {where} AND kind='customresourcedefinitions'"""
            )
            if r["g"] and not is_system_crd_group(r["g"], rules)
        }
    )

    flags = Flags(
        has_workloads=any(ns[n]["workloads"] for n in user),
        stateful=bool(stateful_ns),
        helm=any(ns[n]["helm_releases"] for n in stateful_ns) or bool(helm_labeled & stateful_ns),
        custom_crds=bool(groups),
        hostpath=any(ns[n]["hostpath_workloads"] for n in user) or bool(local_pv),
    )

    def total(key: str, only_user: bool = False) -> int:
        return sum(ns[n][key] for n in (user if only_user else names))

    with transaction(conn):
        conn.execute("DELETE FROM namespace_profile " + where, args)
        conn.executemany(
            "INSERT INTO namespace_profile (site, cluster_id, namespace, is_system, workloads, statefulsets,"
            " pvc_bound, pvc_bytes, hostpath_workloads, helm_releases, lb_services, ingresses, custom_annotations)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    site, cluster_id, n, int(is_system_namespace(n, rules)),
                    ns[n]["workloads"], ns[n]["statefulsets"], ns[n]["pvc_bound"], ns[n]["pvc_bytes"],
                    ns[n]["hostpath_workloads"], ns[n]["helm_releases"], ns[n]["lb_services"],
                    ns[n]["ingresses"], ns[n]["custom_annotations"],
                )
                for n in sorted(names)
            ],
        )  # fmt: skip
        for n, (cpu, mem) in (pod_usage or {}).items():
            conn.execute(
                "UPDATE namespace_profile SET cpu_used_m=?, mem_used_b=? WHERE site=? AND cluster_id=? AND namespace=?",
                (cpu, mem, site, cluster_id, n),
            )
        conn.execute("DELETE FROM cluster_profile " + where, args)
        conn.execute(
            "INSERT INTO cluster_profile (site, cluster_id, class, stateful, helm, custom_crds, hostpath,"
            " statefulsets, pvc_bound, pvc_bytes, helm_releases, crd_groups, lb_services, ingresses,"
            " network, updated_run) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
            (
                site, cluster_id, class_label(flags), int(flags.stateful), int(flags.helm),
                int(flags.custom_crds), int(flags.hostpath), total("statefulsets", True), total("pvc_bound", True),
                total("pvc_bytes", True), total("helm_releases", True), json.dumps(groups), total("lb_services"),
                total("ingresses"), run_id,
            ),
        )  # fmt: skip
    return flags
