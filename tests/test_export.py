import csv
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

from inventory.collector.runner import Scope
from inventory.export.discovery import ANON_SPEC, ExportOptions, build_export, real_names, self_check
from inventory.export.hashing import hash_name

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "reference/k8s-discovery.sh"
SCRIPT_OUT = Path("/tmp/k8sd-local")


def _script_hash(salt: str, type_: str, value: str) -> str:
    """Run the script's own hashing line (it cannot be sourced: it runs on load)."""
    line = next(l for l in SCRIPT.read_text().splitlines() if "printf '%s|%s|%s' \"$SALT\"" in l)
    expr = re.search(r"\$\((printf '%s\|%s\|%s'.*?cut -c1-10)\)", line).group(1)  # the script's pipeline
    out = subprocess.run(
        ["bash", "-c", f'SALT="$1"; k="$2"; v="$3"; SHA="shasum -a 256"; printf "%s-%s" "$k" "$({expr})"',
         "_", salt, type_, value],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return out.stdout


@pytest.mark.parametrize("salt,type_,value", [("s", "cluster", "local"), ("abc123", "ns", "payments"),
                                              ("x", "wl", "name with space|pipe")])
def test_hash_matches_script(salt, type_, value):
    assert hash_name(salt, type_, value) == _script_hash(salt, type_, value)


def _cluster_dirs(d: Path) -> list[Path]:
    """One folder per exported cluster."""
    return [p for p in d.iterdir() if p.is_dir()]


def _local_dir(d: Path, s) -> Path:
    """The bundle folder of the dev `local` cluster, found by its hashed id."""
    from inventory.db.connection import connect

    conn = connect(s.db_path, readonly=True)
    try:
        prof = conn.execute("SELECT cluster_uid FROM cluster_profile WHERE cluster_id='local'").fetchone()
    finally:
        conn.close()
    return d / hash_name(s.export_salt, "cluster", (prof and prof["cluster_uid"]) or "local")


def _extract(path: Path, dest: Path) -> Path:
    with tarfile.open(path) as tf:
        tf.extractall(dest, filter="data")
    return dest


@pytest.fixture(scope="module")
def bundle(collected_db, tmp_path_factory):
    out = tmp_path_factory.mktemp("export")
    s = collected_db.model_copy(update={"export_salt": "test-salt", "export_sensitive_strings": "rancher.local"})
    from inventory.db.connection import connect

    conn = connect(s.db_path)
    try:
        path = build_export(conn, s, ExportOptions(Scope()), out)
        names = real_names(conn, s)
    finally:
        conn.close()
    return path, _extract(path, out / "x"), names, s


pytestmark = pytest.mark.live


def test_export_has_no_real_names(bundle):
    _, d, names, _ = bundle
    assert names  # real node, namespace and workload names exist
    assert self_check(d, ["rancher.local"], names) == []


def test_bundle_layout(bundle):
    path, d, _, _ = bundle
    assert path.name.endswith(".tar.gz")
    assert (d / "fleet_summary.csv").is_file()
    cdirs = _cluster_dirs(d)
    assert cdirs
    for cdir in cdirs:
        assert re.fullmatch(r"cluster-[0-9a-f]{10}", cdir.name)
        for base in ANON_SPEC:
            assert (cdir / f"{base}.csv").is_file(), base
        assert (cdir / "README.txt").is_file()


def test_headers_are_the_scripts(bundle):
    """Header rows equal the column lists in reference/k8s-discovery.sh."""
    _, d, _, _ = bundle
    script = SCRIPT.read_text()
    cdirs = _cluster_dirs(d)
    assert cdirs
    for cdir in cdirs:
        files = sorted(cdir.glob("*.csv"))
        assert files, cdir.name
        for f in files:
            header = next(csv.reader(f.open()))
            assert "\\t".join(header) in script, f.name


def test_headers_equal_script_output(bundle):
    if not SCRIPT_OUT.is_dir():
        pytest.skip(f"no k8s-discovery.sh output in {SCRIPT_OUT}")
    _, d, _, s = bundle
    cdir = _local_dir(d, s)
    [ref] = [p for p in SCRIPT_OUT.iterdir() if p.is_dir()]
    for f in ref.glob("*.csv"):
        if f.name.startswith(("tanzu", "vm_")):
            continue
        assert (cdir / f.name).is_file(), f.name
        assert next(csv.reader(f.open())) == next(csv.reader((cdir / f.name).open())), f.name


def test_summary_row(bundle, collected_db, open_db):
    _, d, _, s = bundle
    rows = list(csv.DictReader((d / "fleet_summary.csv").open()))
    exported = open_db(collected_db.db_path, readonly=True).execute(
        "SELECT count(*) FROM clusters WHERE last_ok_run IS NOT NULL").fetchone()[0]
    assert len(rows) == exported == len(_cluster_dirs(d))
    for r in rows:
        assert r["cluster_id"].startswith("cluster-") and r["context"].startswith("ctx-")
        assert int(r["nodes"]) >= 1
    local = next(r for r in rows if r["cluster_id"] == _local_dir(d, s).name)
    assert int(local["namespaces_total"]) > 10
    assert "stateful" in local["complexity_flags"].split(";")


def test_system_namespaces_readable_user_hashed(bundle):
    _, d, _, s = bundle
    cdir = _local_dir(d, s)
    ns = [r["namespace"] for r in csv.DictReader((cdir / "namespaces.csv").open())]
    assert "kube-system" in ns and "cattle-system" in ns
    assert any(n.startswith("ns-") for n in ns)  # kagent and other user namespaces


def test_name_map_recorded_not_in_bundle(bundle, open_db):
    path, d, _, s = bundle
    with tarfile.open(path) as tf:
        assert not any("name" in m.name and "map" in m.name for m in tf.getmembers())
    conn = open_db(s.db_path)
    rows = conn.execute("SELECT type, value, hashed FROM export_name_map").fetchall()
    assert rows and all(r["hashed"] == hash_name("test-salt", r["type"], r["value"]) for r in rows)


def test_strict_hashes_images_and_charts(collected_db, tmp_path, open_db):
    s = collected_db.model_copy(update={"export_salt": "test-salt", "export_sensitive_strings": ""})
    path = build_export(open_db(s.db_path), s, ExportOptions(Scope(), strict=True), tmp_path)
    d = _extract(path, tmp_path / "x")
    cdir = _local_dir(d, s)
    charts = [r["chart"] for r in csv.DictReader((cdir / "helm_releases.csv").open())]
    assert charts and all(c.startswith("chart-") for c in charts)
    repos = [r["repository"] for r in csv.DictReader((cdir / "images.csv").open())]
    assert repos and all(x.startswith("img-") for x in repos)
    for other in _cluster_dirs(d):  # every cluster's helm charts and image repositories are hashed
        for r in csv.DictReader((other / "helm_releases.csv").open()):
            assert r["chart"].startswith("chart-")
        for r in csv.DictReader((other / "images.csv").open()):
            assert r["repository"].startswith("img-")


def test_planted_ip_blocks(tmp_path):
    c = tmp_path / "cluster-0000000000"
    c.mkdir()
    (c / "nodes.csv").write_text("node,roles\nnode-abc,10.1.2.3\n")
    hits = self_check(tmp_path, [], set())
    assert [(h.file, h.column) for h in hits] == [("cluster-0000000000/nodes.csv", "roles")]
    assert "10.1.2.3" not in hits[0].value_sample


def test_planted_name_and_sensitive_string_block(tmp_path):
    c = tmp_path / "cluster-0000000000"
    c.mkdir()
    (c / "workloads.csv").write_text("namespace,kind,name\nkube-system,Deployment,secret-app\n")
    (c / "images.csv").write_text("registry,repository\nregistry.ExampleCorp.com,app\n")
    hits = self_check(tmp_path, ["examplecorp"], {"secret-app"})
    assert {(h.file.split("/")[-1], h.column) for h in hits} == {("workloads.csv", "name"), ("images.csv", "registry")}


def test_web_export_admin_only(admin_client_collected, viewer_client):
    assert viewer_client.get("/export/discovery").status_code == 403
    assert admin_client_collected.get("/export/discovery").status_code == 200


def test_web_export_needs_salt(admin_client_collected):
    r = admin_client_collected.post("/export/discovery", data={"csrf": admin_client_collected.csrf})
    assert r.status_code == 400 and "EXPORT_SALT" in r.text


def test_cli_export(collected_db, tmp_path, monkeypatch, capsys):
    from inventory.cli import main

    monkeypatch.setenv("DATA_DIR", str(collected_db.data_dir))
    monkeypatch.setenv("EXPORT_SALT", "cli-salt")
    assert main(["export-discovery", "--out", str(tmp_path), "--site", "dev"]) == 0
    assert list(tmp_path.glob("*.tar.gz"))
