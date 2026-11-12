"""Deprecated APIs requested, from the API server /metrics (one API server only)."""

import re

NAME = "deprecated_api_metrics"
_LINE = re.compile(r"^apiserver_requested_deprecated_apis\{([^}]*)\}\s+(\S+)")
_LABEL = re.compile(r'(\w+)="([^"]*)"')


def parse_metrics(text: str) -> dict[str, str]:
    """key group|version|resource|subresource|removed_release -> metric value."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            labels = dict(_LABEL.findall(m.group(1)))
            key = "|".join(labels.get(k, "") for k in ("group", "version", "resource", "subresource", "removed_release"))
            out[key] = m.group(2)
    return out


async def collect(client, cluster_id: str, conn, site: str, run_id: int, cfg: dict) -> None:
    from inventory.collector.optional import write_results

    write_results(conn, site, cluster_id, NAME, run_id, parse_metrics(await client.get_text(f"/k8s/clusters/{cluster_id}/metrics")))
