import asyncio

import pytest

from inventory.collector.members import (
    GRB_OWNER_LABEL,
    ROLETEMPLATE_PATH,
    build_members,
    counts,
    principal_cn,
    read_members,
)
from inventory.rancher.client import RancherClient, RancherError
from inventory.settings import Site

RTS = [
    {"metadata": {"name": "cluster-owner"}, "displayName": "Cluster Owner"},
    {"metadata": {"name": "project-member"}, "displayName": "Project Member"},
]
PROJECTS = [
    {"metadata": {"name": "p-1", "namespace": "c-1"}, "spec": {"clusterName": "c-1", "displayName": "Web"}},
    {"metadata": {"name": "p-2", "namespace": "c-1"}, "spec": {"clusterName": "c-1", "displayName": "Data"}},
]
USERS = [
    {"id": "u-alice", "username": "alice", "name": "Alice Tan", "principalIds": ["local://u-alice"], "enabled": True},
    {"id": "u-bob", "username": "bob", "name": "Bob", "principalIds": ["local://u-bob"], "enabled": False},
    {"id": "u-sys", "username": "sys", "name": "Sys", "principalIds": ["system://x"], "enabled": True},
    {"id": "u-me", "username": "me", "name": "Me", "principalIds": ["local://u-me"], "enabled": True},
]


def crtb(name, cluster, role, **kw):
    return {"metadata": {"name": name, "namespace": cluster, "labels": kw.pop("labels", {})},
            "clusterName": cluster, "roleTemplateName": role, **kw}


def prtb(name, project, role, **kw):
    return {"metadata": {"name": name, "namespace": project.split(":")[1]},
            "projectName": project, "roleTemplateName": role, **kw}


def build(crtbs=(), prtbs=(), users=USERS, self_id=None):
    return build_members(list(crtbs), list(prtbs), PROJECTS, RTS, users, self_id)


def test_principal_cn():
    assert principal_cn("activedirectory_user://CN=Alice Tan,OU=X,DC=y") == "Alice Tan"
    assert principal_cn("local://u-1") is None


def test_cluster_and_project_grants_merge_into_one_member():
    out = build(
        [crtb("a", "c-1", "cluster-owner", userName="u-alice")],
        [prtb("b", "c-1:p-1", "project-member", userName="u-alice"),
         prtb("c", "c-1:p-2", "project-member", userName="u-alice")],
    )
    assert len(out["c-1"]) == 1
    m = out["c-1"][0]
    assert (m.kind, m.subject, m.display_name, m.username) == ("user", "u-alice", "Alice Tan", "alice")
    assert len(m.grants) == 3
    assert m.grants[0] == {"role": "Cluster Owner", "template": "cluster-owner", "scope": "cluster"}
    assert {g["scope"] for g in m.grants} == {"cluster", "project: Web", "project: Data"}


def test_group_member_named_from_cn():
    out = build([crtb("g", "c-1", "cluster-owner",
                      groupPrincipalName="activedirectory_group://CN=Ops-Team,OU=G,DC=x")])
    m = out["c-1"][0]
    assert (m.kind, m.display_name, m.username, m.disabled) == ("group", "Ops-Team", None, False)
    assert m.subject == "activedirectory_group://CN=Ops-Team,OU=G,DC=x"


def test_group_without_cn_uses_principal_after_scheme():
    out = build([crtb("g", "c-1", "x", groupPrincipalName="github_team://org/team")])
    assert out["c-1"][0].display_name == "org/team"


def test_unknown_user_falls_back_to_principal_cn_then_id():
    out = build([
        crtb("a", "c-1", "x", userName="u-zzz", userPrincipalName="activedirectory_user://CN=Zed Z,DC=y"),
        crtb("b", "c-1", "x", userName="u-yyy"),
    ])
    by = {m.subject: m for m in out["c-1"]}
    assert by["u-zzz"].display_name == "Zed Z"
    assert by["u-yyy"].display_name == "u-yyy"
    assert by["u-yyy"].username is None
    # role template unknown -> name used
    assert by["u-yyy"].grants[0]["role"] == "x"


def test_principal_only_binding_resolves_via_principal_ids():
    users = USERS + [{"id": "u-ad", "username": "carol", "name": "Carol",
                      "principalIds": ["local://u-ad", "activedirectory_user://CN=Carol,DC=y"], "enabled": True}]
    out = build([crtb("a", "c-1", "x", userPrincipalName="activedirectory_user://CN=Carol,DC=y")], users=users)
    m = out["c-1"][0]
    assert (m.subject, m.display_name, m.username) == ("u-ad", "Carol", "carol")
    out = build([crtb("a", "c-1", "x", userPrincipalName="activedirectory_user://CN=Dan,DC=y")])
    m = out["c-1"][0]
    assert (m.subject, m.display_name) == ("activedirectory_user://CN=Dan,DC=y", "Dan")


def test_excluded_subjects():
    out = build(
        [crtb("g", "c-1", "x", userName="u-alice", labels={GRB_OWNER_LABEL: "grb-1"}),
         crtb("s", "c-1", "x", userName="u-sys"),
         crtb("sp", "c-1", "x", userPrincipalName="system://other"),
         crtb("m", "c-1", "x", userName="u-me")],
        self_id="u-me",
    )
    assert out.get("c-1", []) == []


def test_duplicate_grants_deduplicated():
    out = build([crtb("a", "c-1", "cluster-owner", userName="u-alice"),
                 crtb("b", "c-1", "cluster-owner", userName="u-alice")])
    assert len(out["c-1"][0].grants) == 1


def test_disabled_user_listed_not_counted():
    out = build([crtb("a", "c-1", "x", userName="u-alice"),
                 crtb("b", "c-1", "x", userName="u-bob"),
                 crtb("c", "c-1", "x", groupPrincipalName="local://g-1")])
    members = out["c-1"]
    assert len(members) == 3
    assert next(m for m in members if m.subject == "u-bob").disabled is True
    assert counts(members) == (1, 1)


def test_order_owner_then_cluster_then_project_then_name():
    out = build(
        [crtb("1", "c-1", "project-member", userName="u-bob"),
         crtb("2", "c-1", "cluster-owner", groupPrincipalName="activedirectory_group://CN=zeta,DC=x"),
         crtb("3", "c-1", "project-member", userName="u-alice")],
        [prtb("4", "c-1:p-1", "project-member", userName="u-me"),
         prtb("5", "c-1:p-1", "project-member", groupPrincipalName="activedirectory_group://CN=Abe,DC=x")],
    )
    assert [m.display_name for m in out["c-1"]] == ["zeta", "Alice Tan", "Bob", "Abe", "Me"]


def test_unknown_project_scope():
    out = build(prtbs=[prtb("a", "c-9:p-x", "project-member", userName="u-alice")])
    assert out["c-9"][0].grants[0]["scope"] == "project: p-x"


def test_malformed_item_skipped():
    out = build(
        [crtb("a", "c-1", "x", userName="u-alice"), "oops",
         {"metadata": {"name": "n"}, "roleTemplateName": "x", "userName": "u-bob"}],
        ["bad", {"metadata": {}, "roleTemplateName": "x", "userName": "u-bob"}],
    )
    assert [m.subject for m in out["c-1"]] == ["u-alice"]
    assert list(out) == ["c-1"]


def test_user_without_name_prefers_principal_cn_over_username():
    users = [{"id": "u-n", "username": "nn", "name": "", "principalIds": ["local://u-n"], "enabled": True},
             {"id": "u-m", "username": "mm", "principalIds": ["local://u-m", "activedirectory_user://CN=Mia M,DC=y"],
              "enabled": True},
             {"id": "u-o", "username": "oo", "principalIds": ["local://u-o"], "enabled": True}]
    out = build([crtb("a", "c-1", "x", userName="u-n", userPrincipalName="activedirectory_user://CN=Nia N,DC=y"),
                 crtb("b", "c-1", "x", userName="u-m"),
                 crtb("c", "c-1", "x", userName="u-o")], users=users)
    by = {m.subject: m.display_name for m in out["c-1"]}
    assert by == {"u-n": "Nia N", "u-m": "Mia M", "u-o": "oo"}


class FakeClient:
    def __init__(self, fail_path=None, status=403):
        self.fail_path, self.status = fail_path, status

    async def list_kind(self, cluster_id, path, page_size=500):
        assert cluster_id == "local"
        if path == self.fail_path:
            raise RancherError(f"HTTP {self.status} for {path}", self.status, path)
        if path.endswith("/clusterroletemplatebindings"):
            return [crtb("b1", "c-1", "cluster-owner", userName="u-alice")]
        if path.endswith("/roletemplates"):
            return RTS
        if path.endswith("/projects"):
            return PROJECTS
        return []

    async def list_users(self):
        return USERS

    async def me(self):
        return {"id": "u-me"}


def test_read_members_ok():
    r = asyncio.run(read_members(FakeClient()))
    assert r.status == "ok"
    assert r.clusters["c-1"]


def test_one_forbidden_call_marks_site_forbidden():
    r = asyncio.run(read_members(FakeClient(ROLETEMPLATE_PATH, 403)))
    assert (r.status, r.clusters) == ("forbidden", {})
    assert r.error


def test_warns_when_most_bound_users_missing_from_user_list(caplog):
    class Client(FakeClient):
        async def list_kind(self, cluster_id, path, page_size=500):
            if path.endswith("/clusterroletemplatebindings"):
                return [crtb(f"b{i}", "c-1", "cluster-owner", userName=f"u-x{i}") for i in range(3)]
            return await super().list_kind(cluster_id, path, page_size)

        async def list_users(self):
            return [USERS[3]]

    with caplog.at_level("WARNING"):
        r = asyncio.run(read_members(Client()))
    assert r.status == "ok"
    assert "README step 5" in caplog.text and "users" in caplog.text
    assert "u-x0" not in caplog.text


def test_no_warning_when_users_present(caplog):
    with caplog.at_level("WARNING"):
        asyncio.run(read_members(FakeClient()))
    assert "README step 5" not in caplog.text


def test_other_error_is_failed():
    r = asyncio.run(read_members(FakeClient(ROLETEMPLATE_PATH, 500)))
    assert r.status == "failed"


def _client(monkeypatch, pages):
    c = RancherClient(Site(name="t", url="https://r.example", token_env="X"), "tok", 5)
    calls = []

    async def get_json(path, params=None, headers=None):
        calls.append((path, params))
        return pages[len(calls) - 1]

    monkeypatch.setattr(c, "get_json", get_json)
    return c, calls


def test_list_users_follows_pagination(monkeypatch):
    c, calls = _client(monkeypatch, [
        {"data": [{"id": "u-1"}], "pagination": {"next": "https://r.example/v3/users?marker=2"}},
        {"data": [{"id": "u-2"}]},
    ])
    users = asyncio.run(c.list_users())
    assert [u["id"] for u in users] == ["u-1", "u-2"]
    assert calls[0][0] == "/v3/users" and calls[1][0] == "/v3/users?marker=2"


def test_list_users_rejects_other_host(monkeypatch):
    c, _ = _client(monkeypatch, [
        {"data": [{"id": "u-1"}], "pagination": {"next": "https://evil.example/v3/users?marker=2"}},
    ])
    with pytest.raises(RancherError, match="another host"):
        asyncio.run(c.list_users())


def test_list_users_detects_loop(monkeypatch):
    nxt = {"next": "https://r.example/v3/users?marker=2"}
    c, _ = _client(monkeypatch, [
        {"data": [{"id": "u-1"}], "pagination": nxt},
        {"data": [{"id": "u-2"}], "pagination": nxt},
        {"data": [{"id": "u-3"}], "pagination": nxt},
    ])
    with pytest.raises(RancherError, match="loop"):
        asyncio.run(c.list_users())


def test_me_and_whoami(monkeypatch):
    c, _ = _client(monkeypatch, [{"data": [{"id": "u-me", "username": "me"}]}] * 2)
    assert asyncio.run(c.me())["id"] == "u-me"
    assert asyncio.run(c.whoami()) == "me"


def test_me_unexpected_shape(monkeypatch):
    c, _ = _client(monkeypatch, [{"data": []}])
    with pytest.raises(RancherError):
        asyncio.run(c.me())


@pytest.mark.live
def test_read_members_dev(live_loop, live_client, dev_clusters):
    r = live_loop.run_until_complete(read_members(live_client))
    assert r.status == "ok"
    assert set(r.clusters) <= {c["id"] for c in dev_clusters}
