import csv
import io
import json
import logging
import shutil

import pytest

from inventory.db.connection import run_readonly_query
from inventory.web import queries
from inventory.web.queries import build_query, validate_filters

pytestmark = pytest.mark.live


@pytest.fixture
def ro(collected_db, open_db):
    return open_db(collected_db.db_path, readonly=True)


def _run(conn, filters):
    sql, params = build_query(filters)
    return run_readonly_query(conn, sql, params)


@pytest.mark.parametrize("f,only_local", [
    ({"service_type": "NodePort"}, True),          # dev has no LoadBalancer service
    ({"address": "192.168.50."}, True),
    ({"address": "192.168.50.210"}, True),
    ({"helm_chart": "cert-manager"}, True),
    ({"helm_chart": "cert-manager@v1.20.2"}, True),
    ({"class": "stateful-helm-crd"}, True),
    ({"flag": "operators"}, True),
    ({"flag": "stateful"}, True),
    ({"annotation": "meta.helm.sh/release-name"}, False),  # any Helm-managed cluster has it
    ({"annotation": "meta.helm.sh/release-namespace=cert-manager"}, True),
    ({"crd_group": "cert-manager.io"}, True),
])
def test_new_filters_find_local(ro, f, only_local):
    rows = _run(ro, f)
    ids = {r["cluster_id"] for r in rows}
    assert rows and "local" in ids, f
    if only_local:
        assert ids == {"local"}, f


@pytest.mark.parametrize("f", [
    {"service_type": "LoadBalancer"}, {"address": "10.255."}, {"helm_chart": "cert-manager@0.0.0"},
    {"class": "stateless"}, {"flag": "gpu"}, {"annotation": "meta.helm.sh/release-namespace=nope"},
    {"crd_group": "example.invalid"},
])
def test_new_filters_can_miss(ro, f):
    assert _run(ro, f) == []


def test_filter_results_are_right(ro):
    rows = _run(ro, {"service_type": "NodePort"})
    assert {r["kind"] for r in rows} == {"services"}
    rows = _run(ro, {"helm_chart": "cert-manager"})
    assert {(r["kind"], r["namespace"]) for r in rows} == {("helmreleases", "cert-manager")}
    rows = _run(ro, {"crd_group": "cert-manager.io"})
    assert rows and all(r["kind"] == "customresourcedefinitions" and r["name"].endswith(".cert-manager.io") for r in rows)


def test_existing_filters(ro):
    by_id = _run(ro, {"cluster": "local", "kind": "namespaces"})
    name = ro.execute("SELECT name FROM clusters WHERE cluster_id='local'").fetchone()[0]
    assert by_id and _run(ro, {"cluster": name, "kind": "namespaces"}) == by_id
    assert {r["name"] for r in _run(ro, {"image": "coredns", "kind": "deployments"})} >= {"rke2-coredns-rke2-coredns"}
    assert _run(ro, {"label": "app.kubernetes.io/managed-by", "kind": "deployments"})
    assert _run(ro, {"label": "app.kubernetes.io/managed-by=Helm", "kind": "deployments"})
    assert _run(ro, {"label": "app.kubernetes.io/managed-by=nope"}) == []


def test_like_wildcards_escaped(ro):
    # Kubernetes names never contain % or _, so escaped wildcards match nothing
    assert _run(ro, {"name": "%"}) == [] and _run(ro, {"name": "_"}) == []
    assert _run(ro, {"image": "%"}) == []


def test_validate_filters():
    validate_filters({"kind": "pods", "flag": "gpu"})
    for bad in ({"data": "x"}, {"flag": "nope"}, {"x": ""}):
        with pytest.raises(ValueError):
            validate_filters(bad)


def test_cap_note_only_when_more_rows(admin_client_collected, monkeypatch):
    from inventory.web.routes import query

    total = len(admin_client_collected.get("/query?kind=namespaces&format=csv").text.strip().splitlines()) - 1
    monkeypatch.setattr(query, "MAX_ROWS", total)
    assert "Showing the first" not in admin_client_collected.get("/query?kind=namespaces").text
    monkeypatch.setattr(query, "MAX_ROWS", total - 1)
    assert "Showing the first" in admin_client_collected.get("/query?kind=namespaces").text


@pytest.mark.parametrize("path, table", [("/workloads", "workloads"), ("/workloads?table=namespaces", "namespaces")])
def test_page_cap_note_only_when_more_rows(admin_client_collected, monkeypatch, path, table):
    sep = "&" if "?" in path else "?"
    csv_path = f"{path}{sep}format=csv"
    total = len(admin_client_collected.get(csv_path).text.strip().splitlines()) - 1
    monkeypatch.setattr(queries, "PAGE_LIMIT", total)
    # the page holds both tables; look at the section of the one under test
    part = 0 if table == "workloads" else 1

    def section():
        return admin_client_collected.get(path).text.split('id="namespaces"')[part]

    assert "Showing the first" not in section()
    monkeypatch.setattr(queries, "PAGE_LIMIT", total - 1)
    assert "Showing the first" in section()
    rows = admin_client_collected.get(csv_path).text.strip().splitlines()
    assert len(rows) - 1 == total - 1


CSV_PAGES = [
    "/clusters", "/inventory", "/network", "/network?table=duplicates", "/helm", "/crds", "/annotations",
    "/workloads", "/storage", "/storage?table=pvs", "/storage?table=snapshots", "/namespaces",
    "/platform", "/platform?table=roles", "/platform?table=api_versions", "/query?kind=pods", "/runs", "/check",
]  # fmt: skip


@pytest.mark.parametrize("path", CSV_PAGES)
def test_csv_on_every_page(admin_client_collected, path):
    sep = "&" if "?" in path else "?"
    r = admin_client_collected.get(f"{path}{sep}format=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv"), path
    assert "attachment" in r.headers["content-disposition"]
    header = next(csv.reader(io.StringIO(r.text)))
    assert header and all(header), path


def test_pages_link_csv(admin_client_collected):
    for path in ("/clusters", "/workloads", "/platform", "/storage"):
        assert "format=csv" in admin_client_collected.get(path).text, path


def test_csv_download_logged(admin_client_collected, caplog):
    with caplog.at_level(logging.INFO, logger="inventory.web"):
        admin_client_collected.get("/workloads?format=csv")
    assert "csv download" in caplog.text and "admin" in caplog.text and "/workloads" in caplog.text


def test_csv_neutralises_formulas():
    from inventory.export.csv import csv_cell

    assert csv_cell("=1+1") == "'=1+1" and csv_cell("-x") == "'-x" and csv_cell("ok") == "ok"
    assert csv_cell(["a", "b"]) == '["a", "b"]' and csv_cell(None) == ""


def test_unknown_csv_table_is_400(admin_client_collected):
    assert admin_client_collected.get("/network?format=csv&table=nope").status_code == 400


def test_export_and_api(admin_client_collected, client):
    r = admin_client_collected.get("/export/clusters.csv")
    assert r.status_code == 200 and "local" in r.text
    r = admin_client_collected.get("/export/resources.csv?kind=namespaces&cluster=local")
    assert r.status_code == 200 and "kube-system" in r.text
    body = admin_client_collected.get("/api/clusters").json()
    assert any(c["cluster_id"] == "local" for c in body)
    body = admin_client_collected.get("/api/resources?kind=namespaces").json()
    assert any(r["name"] == "kube-system" for r in body["rows"]) and body["truncated"] is False
    assert admin_client_collected.get("/api/resources?data=x").status_code == 400
    assert client.get("/api/clusters").status_code == 401
    assert client.get("/export/clusters.csv", follow_redirects=False).status_code == 303


def test_invalid_stored_preset(admin_client_collected, collected_db, tmp_path, open_db):
    from fastapi.testclient import TestClient

    from inventory.web.app import create_app
    from tests.conftest import TEST_PASSWORD, _login, _web_settings

    shutil.copy(collected_db.db_path, tmp_path / "inventory.db")
    s = _web_settings(tmp_path)
    conn = open_db(s.db_path)
    conn.execute("INSERT INTO presets (name, filters, created_at) VALUES ('bad-key', ?, 'x')", (json.dumps({"data": "1"}),))
    conn.execute("INSERT INTO presets (name, filters, created_at) VALUES ('bad-json', '{not json', 'x')")
    conn.execute("INSERT INTO presets (name, filters, created_at) VALUES ('bad-shape', '[{}]', 'x')")
    ids = [r[0] for r in conn.execute("SELECT id FROM presets WHERE name IN ('bad-key', 'bad-json', 'bad-shape')")]
    with TestClient(create_app(s, None)) as c:
        c = _login(c, s, "admin", TEST_PASSWORD)
        assert c.get("/presets").status_code == 200
        for pid in ids:
            r = c.get(f"/check?preset={pid}")
            assert r.status_code == 200 and "invalid preset" in r.text
        r = c.get("/check?preset=abc")
        assert r.status_code == 200 and "not a preset id" in r.text
