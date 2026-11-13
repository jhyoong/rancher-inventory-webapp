"""Machine pools, CNI and CIDRs from provisioning.cattle.io clusters, read through the `local` cluster."""

from dataclasses import dataclass, field

from inventory.rancher.client import RancherClient, RancherError

PROVISIONING_PATH = "/apis/provisioning.cattle.io/v1/clusters"

_ROLES = (("controlPlaneRole", "control-plane"), ("etcdRole", "etcd"), ("workerRole", "worker"))


@dataclass
class MachinePool:
    name: str
    quantity: int | None
    roles: list[str]
    labels: dict[str, str]
    taints: list[str]
    machine_config_kind: str | None


@dataclass
class ClusterProvisioning:
    prov_name: str
    has_rke_config: bool
    pools: list[MachinePool]
    cni: str | None
    cluster_cidr: str | None
    service_cidr: str | None


@dataclass
class ProvisioningResult:
    status: str  # ok | forbidden | failed
    clusters: dict[str, ClusterProvisioning] = field(default_factory=dict)
    error: str | None = None


def _parse_pool(mp: dict) -> MachinePool:
    # Taint values can be sensitive, so only key:effect is kept.
    taints = [f"{t.get('key')}:{t.get('effect')}" for t in mp.get("taints") or []]
    return MachinePool(
        name=mp.get("name") or "",
        quantity=mp.get("quantity"),
        roles=[role for key, role in _ROLES if mp.get(key)],
        labels=dict(mp.get("labels") or {}),
        taints=taints,
        machine_config_kind=(mp.get("machineConfigRef") or {}).get("kind"),
    )


def parse_provisioning(obj: dict) -> tuple[str, ClusterProvisioning] | None:
    """Return (management cluster id, details), or None when the cluster has no status.clusterName."""
    key = (obj.get("status") or {}).get("clusterName")
    if not key:
        return None
    rke = (obj.get("spec") or {}).get("rkeConfig")
    config = (rke or {}).get("machineGlobalConfig") or {}
    return key, ClusterProvisioning(
        prov_name=(obj.get("metadata") or {}).get("name") or "",
        has_rke_config=rke is not None,
        pools=[_parse_pool(mp) for mp in (rke or {}).get("machinePools") or []],
        cni=config.get("cni"),
        cluster_cidr=config.get("cluster-cidr"),
        service_cidr=config.get("service-cidr"),
    )


async def read_provisioning(client: RancherClient) -> ProvisioningResult:
    try:
        items = await client.list_kind("local", PROVISIONING_PATH)
    except RancherError as e:
        return ProvisioningResult("forbidden" if e.status in (401, 403) else "failed", {}, str(e))
    clusters = {}
    for item in items:
        parsed = parse_provisioning(item)
        if parsed:
            clusters[parsed[0]] = parsed[1]
    return ProvisioningResult("ok", clusters)


def pools_status(result: ProvisioningResult | None, cluster_id: str) -> str:
    """not_collected when provisioning was not read; none for clusters without machine pools."""
    if result is None or result.status != "ok":
        return "not_collected"
    found = result.clusters.get(cluster_id)
    return "ok" if found and found.pools else "none"
