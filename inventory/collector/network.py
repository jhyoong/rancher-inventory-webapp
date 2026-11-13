"""Cluster network profile: CNI and CIDRs from provisioning, daemonsets and nodes."""

from inventory.collector.provisioning import ClusterProvisioning
from inventory.collector.rules import Rules


def detect_cni(daemonset_names: list[str], rules: Rules) -> str | None:
    """First CNI, in rules order, that has one of its daemonsets in the cluster."""
    present = set(daemonset_names)
    for cni, names in (rules.network.get("cni_daemonsets") or {}).items():
        if present & set(names):
            return cni
    return None


def cluster_network(conn, site: str, cluster_id: str, prov: ClusterProvisioning | None, rules: Rules) -> dict:
    """Provisioning values win over what is detected from stored daemonsets."""
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM resources WHERE site=? AND cluster_id=? AND kind='daemonsets'", (site, cluster_id)
        )
    ]
    pod_cidrs = sorted(
        {
            r[0]
            for r in conn.execute(
                "SELECT json_extract(data, '$.spec.podCIDR') FROM resources"
                " WHERE site=? AND cluster_id=? AND kind='nodes'",
                (site, cluster_id),
            )
            if r[0]
        }
    )
    if prov and prov.cni:
        cni, source = prov.cni, "provisioning"
    else:
        cni = detect_cni(names, rules)
        source = "daemonset" if cni else None
    return {
        "cni": cni,
        "cni_source": source,
        "cluster_cidr": prov.cluster_cidr if prov else None,
        "service_cidr": prov.service_cidr if prov else None,
        "pod_cidrs": pod_cidrs,
    }
