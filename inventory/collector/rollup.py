"""Namespace roll-up of spec 6.9, with the same counting rules as k8s-discovery.sh."""

from collections import defaultdict

from inventory.db.repo import transaction

ROLLUP_COLUMNS = (
    "pods", "pods_running", "pods_standalone", "restarts", "cpu_req_m", "cpu_lim_m", "mem_req_b", "mem_lim_b",
    "gpu", "deployments", "daemonsets", "jobs", "cronjobs", "pvc_count", "pvc_rwx", "nodeport_services",
    "netpols", "has_quota", "has_limitrange",
)  # fmt: skip


def build_namespace_rollup(conn, site: str, cluster_id: str) -> None:
    """Fill the roll-up columns of the existing namespace_profile rows of one cluster.

    Requests, limits and GPUs count Running and Pending pods only, as the script does.
    """
    ns: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(ROLLUP_COLUMNS, 0))
    args = (site, cluster_id)

    def rows(sql: str):
        return conn.execute(sql, args).fetchall()

    for r in rows(
        """SELECT namespace,
                  json_extract(data, '$.status.phase') AS phase,
                  json_extract(data, '$.owner_kind') AS owner,
                  json_extract(data, '$.restarts') AS restarts,
                  json_extract(data, '$.cpu_req_m') AS cpu_req, json_extract(data, '$.cpu_lim_m') AS cpu_lim,
                  json_extract(data, '$.mem_req_b') AS mem_req, json_extract(data, '$.mem_lim_b') AS mem_lim,
                  json_extract(data, '$.gpu') AS gpu
           FROM resources WHERE site=? AND cluster_id=? AND kind='pods'"""
    ):
        p = ns[r["namespace"]]
        p["pods"] += 1
        p["pods_running"] += r["phase"] == "Running"
        p["pods_standalone"] += r["owner"] is None
        p["restarts"] += r["restarts"] or 0
        if r["phase"] in ("Running", "Pending"):
            p["cpu_req_m"] += r["cpu_req"] or 0
            p["cpu_lim_m"] += r["cpu_lim"] or 0
            p["mem_req_b"] += r["mem_req"] or 0
            p["mem_lim_b"] += r["mem_lim"] or 0
            p["gpu"] += r["gpu"] or 0

    for r in rows(
        """SELECT namespace, kind, count(*) AS c FROM resources
           WHERE site=? AND cluster_id=? AND kind IN ('deployments', 'daemonsets', 'jobs', 'cronjobs')
           GROUP BY namespace, kind"""
    ):
        ns[r["namespace"]][r["kind"]] = r["c"]

    for r in rows(
        """SELECT namespace, count(*) AS c,
                  sum(EXISTS (SELECT 1 FROM json_each(data, '$.access_modes') WHERE value='ReadWriteMany')) AS rwx
           FROM resources WHERE site=? AND cluster_id=? AND kind='persistentvolumeclaims' GROUP BY namespace"""
    ):
        ns[r["namespace"]]["pvc_count"] = r["c"]
        ns[r["namespace"]]["pvc_rwx"] = r["rwx"]

    for r in rows(
        """SELECT namespace, count(*) AS c FROM resources
           WHERE site=? AND cluster_id=? AND kind='services' AND json_extract(data, '$.spec.type')='NodePort'
           GROUP BY namespace"""
    ):
        ns[r["namespace"]]["nodeport_services"] = r["c"]

    for kind, col, presence in (
        ("networkpolicies", "netpols", False),
        ("resourcequotas", "has_quota", True),
        ("limitranges", "has_limitrange", True),
    ):
        for r in conn.execute(
            "SELECT namespace, count(*) AS c FROM resources WHERE site=? AND cluster_id=? AND kind=? GROUP BY namespace",
            (*args, kind),
        ):
            ns[r["namespace"]][col] = 1 if presence else r["c"]

    names = [r[0] for r in rows("SELECT namespace FROM namespace_profile WHERE site=? AND cluster_id=?")]
    sets = ", ".join(f"{c}=?" for c in ROLLUP_COLUMNS)
    with transaction(conn):
        conn.executemany(
            f"UPDATE namespace_profile SET {sets} WHERE site=? AND cluster_id=? AND namespace=?",
            [(*(ns[n][c] for c in ROLLUP_COLUMNS), site, cluster_id, n) for n in names],
        )
