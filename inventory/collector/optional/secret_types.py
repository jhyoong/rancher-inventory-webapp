"""Count of Secrets per type. Uses the server-side table view, so no Secret data is transferred."""

NAME = "secret_types"
TABLE = {"Accept": "application/json;as=Table;v=v1;g=meta.k8s.io"}


async def collect(client, cluster_id: str, conn, site: str, run_id: int, cfg: dict) -> None:
    from inventory.collector.optional import write_results

    counts: dict[str, int] = {}
    token = ""
    seen: set[str] = set()
    while True:
        params = {"limit": 500, "includeObject": "None", **({"continue": token} if token else {})}
        body = await client.get_json(f"/k8s/clusters/{cluster_id}/api/v1/secrets", params, headers=TABLE)
        cols = [c.get("name") for c in body.get("columnDefinitions") or []]
        idx = cols.index("Type") if "Type" in cols else None
        for row in body.get("rows") or []:
            cells = row.get("cells") or []
            t = cells[idx] if idx is not None and idx < len(cells) else "unknown"
            counts[str(t)] = counts.get(str(t), 0) + 1
        token = (body.get("metadata") or {}).get("continue") or ""
        if not token or token in seen:
            break
        seen.add(token)
    write_results(conn, site, cluster_id, NAME, run_id, counts)
