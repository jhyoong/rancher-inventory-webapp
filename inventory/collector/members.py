"""Rancher members of each downstream cluster, built from role template bindings (pure logic)."""

import logging
import re
from dataclasses import dataclass, field

from inventory.rancher.client import RancherClient, RancherError

log = logging.getLogger(__name__)

GRB_OWNER_LABEL = "authz.management.cattle.io/grb-owner"
OWNER_TEMPLATE = "cluster-owner"

_MGMT = "/apis/management.cattle.io/v3"
CRTB_PATH = f"{_MGMT}/clusterroletemplatebindings"
PRTB_PATH = f"{_MGMT}/projectroletemplatebindings"
PROJECT_PATH = f"{_MGMT}/projects"

_CN = re.compile(r"(?:^|[,/])CN=((?:\\.|[^,])+)", re.IGNORECASE)


@dataclass
class Member:
    kind: str  # user | group
    subject: str
    display_name: str
    username: str | None
    disabled: bool
    grants: list[dict]  # {"role", "template", "scope"}


@dataclass
class MembersResult:
    status: str  # ok | forbidden | failed
    clusters: dict[str, list[Member]] = field(default_factory=dict)
    error: str | None = None


def principal_cn(principal: str) -> str | None:
    """CN from an LDAP-style principal id, or None."""
    m = _CN.search(principal.split("://", 1)[-1])
    return m.group(1).replace("\\", "") if m else None


def _after_scheme(principal: str) -> str:
    return principal.split("://", 1)[-1]


def _is_system(principal_ids: list) -> bool:
    return bool(principal_ids) and all(str(p).startswith("system://") for p in principal_ids)


def _grant_template(binding: dict) -> str:
    template = binding["roleTemplateName"]
    if not isinstance(template, str) or not template:
        raise ValueError("missing roleTemplateName")
    return template


def _subject(binding: dict, users_by_id: dict, users_by_principal: dict):
    """Return (kind, subject, user dict or None, principal or None); raises ValueError when no subject."""
    user_id = binding.get("userName")
    user_principal = binding.get("userPrincipalName")
    group = binding.get("groupPrincipalName") or binding.get("groupName")
    if user_id:
        return "user", user_id, users_by_id.get(user_id), user_principal
    if user_principal:
        found = users_by_principal.get(user_principal)
        if found:
            return "user", found["id"], found, user_principal
        return "user", user_principal, None, user_principal
    if group:
        return "group", group, None, group
    raise ValueError("binding has no subject")


def _user_display(user: dict | None, principal: str | None, subject: str) -> str:
    """name, then a CN from the principal (or the user's own principals), then username, then id."""
    if user and user.get("name"):
        return user["name"]
    candidates = [principal] if principal else []
    candidates += [p for p in (user or {}).get("principalIds") or [] if isinstance(p, str)]
    for p in candidates:
        cn = principal_cn(p)
        if cn:
            return cn
    if user:
        if user.get("username"):
            return user["username"]
        if user.get("id"):
            return user["id"]
    if principal and subject == principal:
        return _after_scheme(principal)
    return subject


def build_members(
    crtbs: list[dict],
    prtbs: list[dict],
    projects: list[dict],
    roletemplates: list[dict],
    users: list[dict],
    self_id: str | None,
) -> dict[str, list[Member]]:
    users_by_id: dict[str, dict] = {}
    users_by_principal: dict[str, dict] = {}
    for u in users:
        try:
            users_by_id[u["id"]] = u
            for p in u.get("principalIds") or []:
                users_by_principal.setdefault(p, u)
        except (KeyError, TypeError, AttributeError) as e:
            log.warning("skipped malformed user: %r", e)

    role_names: dict[str, str] = {}
    for rt in roletemplates:
        try:
            # /v3 (Norman) shape: id + name (the display name); k8s shape: metadata.name + displayName.
            key = rt["id"] if "id" in rt else rt["metadata"]["name"]
            role_names[key] = rt.get("displayName") or rt.get("name") or key
        except (KeyError, TypeError, AttributeError) as e:
            log.warning("skipped malformed roletemplate: %r", e)

    project_names: dict[str, str] = {}
    for p in projects:
        try:
            meta = p["metadata"]
            cluster = meta.get("namespace") or (p.get("spec") or {}).get("clusterName")
            display = (p.get("spec") or {}).get("displayName")
            if cluster and display:
                project_names[f"{cluster}:{meta['name']}"] = display
        except (KeyError, TypeError, AttributeError) as e:
            log.warning("skipped malformed project: %r", e)

    # cluster id -> (kind, subject) -> Member
    found: dict[str, dict[tuple[str, str], Member]] = {}

    def add(cluster: str, binding: dict, scope: str) -> None:
        kind, subject, user, principal = _subject(binding, users_by_id, users_by_principal)
        if kind == "user":
            if user and _is_system(user.get("principalIds") or []):
                return
            if subject.startswith("system://") or (principal or "").startswith("system://"):
                return
            if self_id and subject == self_id:
                return
        template = _grant_template(binding)
        grant = {"role": role_names.get(template, template), "template": template, "scope": scope}
        member = found.setdefault(cluster, {}).get((kind, subject))
        if member is None:
            if kind == "user":
                member = Member("user", subject, _user_display(user, principal, subject),
                                (user or {}).get("username"), (user or {}).get("enabled") is False, [])
            else:
                member = Member("group", subject, principal_cn(subject) or _after_scheme(subject), None, False, [])
            found[cluster][(kind, subject)] = member
        if grant not in member.grants:
            member.grants.append(grant)

    def labelled_grb(binding: dict) -> bool:
        return GRB_OWNER_LABEL in ((binding.get("metadata") or {}).get("labels") or {})

    for b in crtbs:
        try:
            if labelled_grb(b):
                continue
            cluster = b["clusterName"]
            if not cluster:
                raise ValueError("missing clusterName")
            add(cluster, b, "cluster")
        except (KeyError, TypeError, AttributeError, ValueError) as e:
            log.warning("skipped malformed cluster role binding: %r", e)

    for b in prtbs:
        try:
            if labelled_grb(b):
                continue
            cluster, sep, project = b["projectName"].partition(":")
            if not cluster or not sep or not project:
                raise ValueError("bad projectName")
            add(cluster, b, f"project: {project_names.get(f'{cluster}:{project}', project)}")
        except (KeyError, TypeError, AttributeError, ValueError) as e:
            log.warning("skipped malformed project role binding: %r", e)

    def sort_key(m: Member):
        owner = any(g["template"] == OWNER_TEMPLATE and g["scope"] == "cluster" for g in m.grants)
        cluster_scope = any(g["scope"] == "cluster" for g in m.grants)
        return (0 if owner else 1 if cluster_scope else 2, m.display_name.casefold())

    return {c: sorted(ms.values(), key=sort_key) for c, ms in found.items() if ms}


def counts(members: list[Member]) -> tuple[int, int]:
    """(enabled users, groups)."""
    return (
        sum(1 for m in members if m.kind == "user" and not m.disabled),
        sum(1 for m in members if m.kind == "group"),
    )


def _warn_missing_users(crtbs: list[dict], prtbs: list[dict], users: list[dict]) -> None:
    """Without the "users" permission /v3/users returns almost nothing and names degrade; say so (counts only)."""
    known = {u.get("id") for u in users if isinstance(u, dict)}
    bound = {b["userName"] for b in [*crtbs, *prtbs] if isinstance(b, dict) and b.get("userName")}
    missing = len(bound - known)
    if bound and missing * 2 > len(bound):
        log.warning(
            "%d of %d users bound on clusters are missing from /v3/users; member names may be incomplete. "
            'Check that the inventory-reader rule includes "users" (README step 5).',
            missing, len(bound),
        )


async def read_members(client: RancherClient) -> MembersResult:
    try:
        crtbs = await client.list_kind("local", CRTB_PATH)
        prtbs = await client.list_kind("local", PRTB_PATH)
        projects = await client.list_kind("local", PROJECT_PATH)
        roletemplates = await client.list_roletemplates()
        users = await client.list_users()
        self_id = (await client.me()).get("id")
    except RancherError as e:
        return MembersResult("forbidden" if e.status in (401, 403) else "failed", {}, str(e))
    _warn_missing_users(crtbs, prtbs, users)
    return MembersResult("ok", build_members(crtbs, prtbs, projects, roletemplates, users, self_id))
