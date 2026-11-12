"""Instance count per CRD, for the API groups listed in rules.yaml. One call per CRD with limit=1."""

import json

from inventory.rancher.client import RancherError

NAME = "cr_counts"


async def collect(client, cluster_id: str, conn, site: str, run_id: int, cfg: dict) -> None:
    from inventory.collector.optional import write_results

    groups = set(cfg.get("groups") or [])
    counts: dict[str, int] = {}
    for name, data in conn.execute(
        "SELECT name, data FROM resources WHERE site=? AND cluster_id=? AND kind='customresourcedefinitions'",
        (site, cluster_id),
    ).fetchall():
        spec = json.loads(data).get("spec") or {}
        if spec.get("group") not in groups:
            continue
        version = next((v["name"] for v in spec.get("versions") or [] if v.get("storage")), None)
        plural = name.split(".", 1)[0]
        if not version:
            continue
        try:
            body = await client.get_json(
                f"/k8s/clusters/{cluster_id}/apis/{spec['group']}/{version}/{plural}", {"limit": 1}
            )
        except RancherError as e:
            if e.status in (401, 403, 404):  # no rule for this CRD, or it went away: count unknown
                continue
            raise
        meta = body.get("metadata") or {}
        counts[name] = len(body.get("items") or []) + int(meta.get("remainingItemCount") or 0)
    write_results(conn, site, cluster_id, NAME, run_id, counts)
