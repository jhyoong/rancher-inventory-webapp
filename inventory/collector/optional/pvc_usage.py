"""Used bytes per PVC from kubelet stats (nodes/proxy). One call per node."""

NAME = "pvc_usage"


def parse_summary(body: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for pod in body.get("pods") or []:
        for vol in pod.get("volume") or []:
            ref = vol.get("pvcRef") or {}
            if ref.get("name") and vol.get("usedBytes") is not None:
                out[f"{ref.get('namespace')}/{ref['name']}"] = int(vol["usedBytes"])
    return out


async def collect(client, cluster_id: str, conn, site: str, run_id: int, cfg: dict) -> None:
    from inventory.collector.optional import write_results

    nodes = [r[0] for r in conn.execute(
        "SELECT name FROM resources WHERE site=? AND cluster_id=? AND kind='nodes'", (site, cluster_id))]  # fmt: skip
    used: dict[str, int] = {}
    for node in nodes:
        used.update(parse_summary(await client.get_json(f"/k8s/clusters/{cluster_id}/api/v1/nodes/{node}/proxy/stats/summary")))
    write_results(conn, site, cluster_id, NAME, run_id, used)
