import asyncio
import logging
import os
import shutil

import pytest

from inventory.collector.runner import Scope, create_run, execute_run
from inventory.collector.rules import load_rules

pytestmark = pytest.mark.live

OFF_PATHS = ("/proxy/stats", "/api/v1/secrets")


def _settings_with(settings, tmp_path, block: str):
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    shutil.copy(settings.sites_file, cfg / "sites.yaml")
    (cfg / "rules.yaml").write_text("optional_collectors:\n" + block)
    return settings.model_copy(update={"sites_file": cfg / "sites.yaml"})


def _run(s, db_path, open_db):
    conn = open_db(db_path)
    rid = create_run(conn, "manual", "t", Scope())
    status = asyncio.run(execute_run(s, rid))
    return conn, status


def _results(conn, collector):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT key, value FROM optional_results WHERE cluster_id='local' AND collector=?", [collector])}


def test_defaults_all_off(settings):
    oc = load_rules(settings).optional_collectors
    assert set(oc) == {"cr_counts", "pvc_usage", "deprecated_api_metrics", "secret_types", "nginx_config_keys"}
    assert not any(c["enabled"] for c in oc.values())


def test_unknown_collector_rejected(settings, tmp_path):
    from inventory.settings import ConfigError

    s = _settings_with(settings, tmp_path, "  helm_magic: {enabled: true}\n")
    with pytest.raises(ConfigError, match="helm_magic"):
        load_rules(s)


def test_all_off_makes_no_calls(settings, db, db_path, open_db, caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        conn, status = _run(settings, db_path, open_db)
    assert status == "ok"
    urls = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert len(urls) > 30  # the run itself was logged
    for u in urls:
        url = u.split(" ")[3] if len(u.split(" ")) > 3 else u
        path = url.split("?")[0]
        assert not path.endswith("/metrics") and not any(p in path for p in OFF_PATHS), u
    assert conn.execute("SELECT count(*) FROM optional_results").fetchone()[0] == 0


def test_cr_counts(settings, db, db_path, open_db, tmp_path, dev_lists):
    s = _settings_with(settings, tmp_path, "  cr_counts: {enabled: true, groups: [catalog.cattle.io]}\n")
    conn, status = _run(s, db_path, open_db)
    assert status == "ok"
    res = _results(conn, "cr_counts")
    assert res == {"apps.catalog.cattle.io": str(len(dev_lists["helmreleases"]))}


def test_nginx_config_keys(settings, db, db_path, open_db, tmp_path, live_loop, live_client):
    s = _settings_with(settings, tmp_path, "  nginx_config_keys: {enabled: true}\n")
    conn, status = _run(s, db_path, open_db)
    assert status == "ok"
    res = _results(conn, "nginx_config_keys")
    cm = live_loop.run_until_complete(live_client.get_json(
        "/k8s/clusters/local/api/v1/namespaces/kube-system/configmaps/rke2-ingress-nginx-controller"))
    assert res["kube-system/rke2-ingress-nginx-controller"] == ";".join(sorted(cm.get("data") or {}))
    for value in res.values():  # keys only
        assert all(v not in value for v in (cm.get("data") or {}).values() if v)


def test_not_permitted_optins_do_not_fail_the_run(settings, db, db_path, open_db, tmp_path, caplog):
    if any(os.environ.get(f"OPTIN_{n.upper()}") for n in ("pvc_usage", "deprecated_api_metrics", "secret_types")):
        pytest.skip("extended RBAC is bound; this test needs the base role")
    block = "  pvc_usage: {enabled: true}\n  deprecated_api_metrics: {enabled: true}\n  secret_types: {enabled: true}\n"
    s = _settings_with(settings, tmp_path, block)
    with caplog.at_level(logging.INFO, logger="inventory.collector.cluster"):
        conn, status = _run(s, db_path, open_db)
    assert status == "ok"
    assert conn.execute("SELECT count(*) FROM optional_results").fetchone()[0] == 0
    for name in ("pvc_usage", "deprecated_api_metrics", "secret_types"):
        assert f"opt-in {name} not permitted" in caplog.text, name


@pytest.mark.parametrize("name,block", [
    ("pvc_usage", "  pvc_usage: {enabled: true}\n"),
    ("deprecated_api_metrics", "  deprecated_api_metrics: {enabled: true}\n"),
    ("secret_types", "  secret_types: {enabled: true}\n"),
])
def test_optin_with_extended_rbac(settings, db, db_path, open_db, tmp_path, name, block):
    if not os.environ.get(f"OPTIN_{name.upper()}"):
        pytest.skip(f"set OPTIN_{name.upper()}=1 after binding its rule from deploy/inventory-reader-extended.yaml")
    s = _settings_with(settings, tmp_path, block)
    conn, status = _run(s, db_path, open_db)
    assert status == "ok"
    res = _results(conn, name)
    if name == "secret_types":
        assert res and all(int(v) > 0 for v in res.values())
    elif name == "pvc_usage":
        assert res and all(k.count("/") == 1 and int(v) >= 0 for k, v in res.items())
    else:
        assert all("|" in k for k in res)


def test_home_lists_active_optins(admin_client, web_settings, tmp_path):
    cfg = web_settings.sites_file.parent
    assert "Opt-in collectors: none active" in admin_client.get("/").text
    s = _settings_with(web_settings, tmp_path, "  cr_counts: {enabled: true, groups: [catalog.cattle.io]}\n")
    admin_client.app.state.settings = s
    assert "Opt-in collectors: cr_counts" in admin_client.get("/").text
    assert cfg.exists()
