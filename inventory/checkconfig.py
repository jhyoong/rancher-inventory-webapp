import asyncio
import ssl
from dataclasses import dataclass, field
from datetime import date

from inventory.collector.kinds import load_kinds
from inventory.rancher.client import RancherClient, RancherError
from inventory.settings import ConfigError, Settings, Site, load_sites, mask_token, site_token


@dataclass
class SiteCheck:
    site: Site
    reachable: bool = False
    tls: str = "unknown"
    user: str | None = None
    token_days_left: int | None = None
    clusters: int | None = None
    sample_cluster: str | None = None
    kinds: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    token_masked: str = ""

    @property
    def ok(self) -> bool:
        return self.reachable and self.user is not None and self.sample_cluster is not None


def _is_tls_error(e: RancherError) -> bool:
    ctx = e.__context__
    while ctx is not None:
        if isinstance(ctx, ssl.SSLError) or "CERTIFICATE_VERIFY" in str(ctx):
            return True
        ctx = ctx.__cause__ or ctx.__context__
    return False


async def _check_site(site: Site, settings: Settings, kinds) -> SiteCheck:
    check = SiteCheck(site=site)
    try:
        await _run_checks(check, site, settings, kinds)
    except Exception as e:  # one bad site must not hide the others
        check.errors.append(f"unexpected failure: {type(e).__name__}")
    return check


async def _run_checks(check: SiteCheck, site: Site, settings: Settings, kinds) -> None:
    if site.token_expires:
        check.token_days_left = (site.token_expires - date.today()).days
    if not site.tls_verify:
        check.tls = "skipped"
    try:
        token = site_token(site)
    except ConfigError as e:
        check.errors.append(str(e))
        return
    check.token_masked = mask_token(token)
    async with RancherClient(site, token, settings.request_timeout_s) as client:
        try:
            check.user = await client.whoami()
            check.reachable = True
            if site.tls_verify:
                check.tls = "verified"
        except RancherError as e:
            if e.status is None:
                if _is_tls_error(e):
                    check.tls = f"error: {e}"
                check.errors.append(str(e))
                return
            check.reachable = True
            if site.tls_verify:
                check.tls = "verified"
            check.errors.append(f"token not accepted: {e}")
            return
        try:
            clusters = await client.list_clusters()
        except RancherError as e:
            check.errors.append(f"cannot list clusters: {e}")
            return
        check.clusters = len(clusters)
        active = [c for c in clusters if c.get("state") == "active"]
        sample = next((c for c in active if c.get("id") == "local"), None)
        if sample is None and active:
            sample = active[0]
        if sample is None:
            check.errors.append("no active cluster to sample")
            return
        cid = sample["id"]
        check.sample_cluster = cid

        async def probe(name: str, path: str) -> None:
            try:
                await client.list_kind(cid, path, page_size=1)
                check.kinds[name] = 200
            except RancherError as e:
                if e.status is None:
                    check.errors.append(f"{name}: {e}")
                else:
                    check.kinds[name] = e.status

        await asyncio.gather(*(probe(k.name, k.path) for k in kinds.values()))


async def check_config(settings: Settings) -> list[SiteCheck]:
    sites = load_sites(settings)
    kinds = load_kinds(settings)
    return list(await asyncio.gather(*(_check_site(s, settings, kinds) for s in sites)))


def format_report(checks: list[SiteCheck]) -> str:
    lines: list[str] = []
    for c in checks:
        lines.append(f"Site: {c.site.name} ({c.site.url})")
        lines.append(f"  reachable: {'yes' if c.reachable else 'no'}")
        lines.append(f"  tls: {c.tls}")
        if not c.site.tls_verify:
            lines.append("  WARNING: TLS verification is off")
        lines.append(f"  token: {c.token_masked or '(not set)'}")
        lines.append(f"  user: {c.user or '(token not valid)'}")
        if c.token_days_left is not None:
            lines.append(f"  token days left: {c.token_days_left}")
            if c.token_days_left < 30:
                lines.append("  WARNING: token expires in less than 30 days")
        if c.clusters is not None:
            lines.append(f"  clusters visible: {c.clusters}")
        if c.sample_cluster:
            lines.append(f"  sample cluster: {c.sample_cluster}")
            ok = sorted(k for k, s in c.kinds.items() if s == 200)
            lines.append(f"  kinds 200 ({len(ok)}): {', '.join(ok)}")
            for status in sorted({s for s in c.kinds.values() if s != 200}):
                names = sorted(k for k, s in c.kinds.items() if s == status)
                lines.append(f"  kinds {status} ({len(names)}): {', '.join(names)}")
        for err in c.errors:
            lines.append(f"  ERROR: {err}")
        lines.append("")
    return "\n".join(lines)
