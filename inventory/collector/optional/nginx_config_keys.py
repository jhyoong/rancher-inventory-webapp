"""Keys set in ingress-nginx ConfigMaps. Values are read in memory and never stored."""

NAME = "nginx_config_keys"


async def collect(client, cluster_id: str, conn, site: str, run_id: int, cfg: dict) -> None:
    from inventory.collector.optional import write_results

    cms = conn.execute(
        "SELECT namespace, name FROM resources WHERE site=? AND cluster_id=? AND kind='configmaps'"
        " AND name LIKE '%nginx%' ORDER BY namespace, name",
        (site, cluster_id),
    ).fetchall()
    keys: dict[str, str] = {}
    for ns, name in cms:
        body = await client.get_json(f"/k8s/clusters/{cluster_id}/api/v1/namespaces/{ns}/configmaps/{name}")
        keys[f"{ns}/{name}"] = ";".join(sorted(body.get("data") or {}))
        del body  # values are not kept
    write_results(conn, site, cluster_id, NAME, run_id, keys)
