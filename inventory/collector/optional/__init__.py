"""Opt-in collectors (spec 6.12). Each module has NAME and `async collect(...)`.

Only collectors with `enabled: true` in rules.yaml run. They write to optional_results.
"""

import logging

from inventory.collector.optional import (
    cr_counts,
    deprecated_api_metrics,
    nginx_config_keys,
    pvc_usage,
    secret_types,
)
from inventory.collector.rules import Rules
from inventory.db.repo import transaction
from inventory.rancher.client import RancherError

log = logging.getLogger("inventory.collector.cluster")

COLLECTORS = {m.NAME: m for m in (cr_counts, pvc_usage, deprecated_api_metrics, secret_types, nginx_config_keys)}


def active(rules: Rules) -> list[str]:
    return [n for n in COLLECTORS if rules.optional_collectors.get(n, {}).get("enabled")]


def write_results(conn, site: str, cluster_id: str, collector: str, run_id: int, values: dict[str, str]) -> None:
    """Replace one collector's results for a cluster."""
    with transaction(conn):
        conn.execute(
            "DELETE FROM optional_results WHERE site=? AND cluster_id=? AND collector=?", (site, cluster_id, collector)
        )
        conn.executemany(
            "INSERT INTO optional_results (site, cluster_id, collector, key, value, run_id) VALUES (?, ?, ?, ?, ?, ?)",
            [(site, cluster_id, collector, k, str(v), run_id) for k, v in sorted(values.items())],
        )


async def run_optional(client, conn, site: str, cluster_id: str, run_id: int, rules: Rules) -> None:
    """Run the enabled collectors. Errors are logged; they never fail the cluster."""
    for name in active(rules):
        try:
            await COLLECTORS[name].collect(client, cluster_id, conn, site, run_id, rules.optional_collectors[name])
        except RancherError as e:
            if e.status in (401, 403):
                log.info("opt-in %s not permitted on %s/%s: %s", name, site, cluster_id, e)
            else:
                log.warning("opt-in %s failed on %s/%s: %s", name, site, cluster_id, e)
        except Exception:
            log.exception("opt-in %s failed on %s/%s", name, site, cluster_id)
