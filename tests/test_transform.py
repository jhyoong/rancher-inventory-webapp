import json

import pytest

from inventory.collector.transform import trim

pytestmark = pytest.mark.live


def test_helm_values_never_kept(dev_lists, kinds):
    assert dev_lists["helmreleases"]
    for o in dev_lists["helmreleases"]:
        t = trim(kinds["helmreleases"], o)
        assert "values" not in json.dumps(t.data["spec"]["chart"]) and "values" not in t.data["spec"]
        assert t.data["spec"]["chart"]["metadata"]["name"]
        for r in t.data["spec"].get("resources", []):
            assert set(r) <= {"apiVersion", "kind"}


def test_crd_schema_dropped(dev_lists, kinds):
    assert dev_lists["customresourcedefinitions"]
    for o in dev_lists["customresourcedefinitions"]:
        for v in trim(kinds["customresourcedefinitions"], o).data["spec"]["versions"]:
            assert set(v) <= {"name", "served", "storage", "deprecated"}


def test_configmap_has_no_data(dev_lists, kinds):
    assert dev_lists["configmaps"]
    for o in dev_lists["configmaps"]:
        assert trim(kinds["configmaps"], o).data == {}


def test_node_roles(dev_lists, kinds):
    roles = [trim(kinds["nodes"], o).data["roles"] for o in dev_lists["nodes"]]
    assert all(isinstance(r, list) for r in roles)
    assert any("control-plane" in r for r in roles)


def test_images_found(dev_lists, kinds):
    assert dev_lists["deployments"]
    for o in dev_lists["deployments"]:
        assert len(trim(kinds["deployments"], o).images) >= 1


def test_hash_stable(dev_lists, kinds):
    for kind, items in dev_lists.items():
        for o in items:
            assert trim(kinds[kind], o).hash == trim(kinds[kind], o).hash


def test_workload_pvc_and_pod_requests(dev_lists, kinds):
    for o in dev_lists["deployments"]:
        d = trim(kinds["deployments"], o).data
        assert isinstance(d["pvc_claims"], list) and isinstance(d["hostpath"], bool)
    for o in dev_lists["statefulsets"]:
        for tpl in trim(kinds["statefulsets"], o).data["volume_claim_templates"]:
            assert set(tpl) == {"name", "storage_class", "size"}
    for o in dev_lists["pods"]:
        d = trim(kinds["pods"], o).data
        assert d["cpu_req_m"] >= 0 and d["mem_req_b"] >= 0
    for o in dev_lists["persistentvolumes"]:
        assert isinstance(trim(kinds["persistentvolumes"], o).data["local"], bool)


def test_no_container_commands_or_env_stored(dev_lists, kinds):
    for kind, items in dev_lists.items():
        for o in items:
            blob = json.dumps(trim(kinds[kind], o).data)
            for word in ('"env"', '"envFrom"', '"command"', '"args"'):
                assert word not in blob, kind


def test_counts_match_rancher(collected_db, dev_lists, open_db):
    from inventory.db.connection import connect

    conn = open_db(collected_db.db_path)
    for kind, items in dev_lists.items():
        n = conn.execute("SELECT count(*) FROM resources WHERE kind=?", [kind]).fetchone()[0]
        assert n == len(items), kind


def test_crd_and_helmrelease_non_list_dropped(dev_lists, kinds):
    """Real objects with the list field replaced by a string store nothing for it."""
    import copy

    crd = copy.deepcopy(dev_lists["customresourcedefinitions"][0])
    crd["spec"]["versions"] = "v1"
    assert "versions" not in trim(kinds["customresourcedefinitions"], crd).data.get("spec", {})
    app = copy.deepcopy(dev_lists["helmreleases"][0])
    app["spec"]["resources"] = "x"
    assert "resources" not in trim(kinds["helmreleases"], app).data.get("spec", {})
