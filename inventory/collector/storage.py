"""Derived storage fields (spec 6.10). Paths, NFS servers and parameter values are never kept."""

_PV_SOURCES = ("nfs", "hostPath", "local", "vsphereVolume", "iscsi")


def derive_pv(obj: dict) -> dict:
    spec = obj.get("spec") or {}
    driver = (spec.get("csi") or {}).get("driver")
    source = f"csi:{driver}" if driver else next((s for s in _PV_SOURCES if spec.get(s)), "other")
    return {
        "local": bool(spec.get("local") or spec.get("hostPath")),
        "access_modes": list(spec.get("accessModes") or []),
        "volume_mode": spec.get("volumeMode") or "Filesystem",
        "reclaim": spec.get("persistentVolumeReclaimPolicy"),
        "source": source,
        "pinned": bool(spec.get("nodeAffinity")),
    }


def derive_pvc(
    obj: dict, mounted_claims: set[tuple[str, str]] | None, used_by: dict[tuple[str, str], list[str]] | None
) -> dict:
    """mounted and used_by come from the pod PVC claims of the same cluster; None when pods were not listed."""
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    key = (meta.get("namespace") or "", meta.get("name") or "")
    return {
        "access_modes": list(spec.get("accessModes") or []),
        "volume_mode": spec.get("volumeMode") or "Filesystem",
        "volume_name": spec.get("volumeName"),
        "data_source_kind": (spec.get("dataSource") or {}).get("kind"),
        "mounted": None if mounted_claims is None else key in mounted_claims,
        "used_by": None if used_by is None else sorted(used_by.get(key, [])),
    }


def derive_storageclass(obj: dict) -> dict:
    ann = (obj.get("metadata") or {}).get("annotations") or {}
    return {
        "allow_expansion": bool(obj.get("allowVolumeExpansion")),
        "default": ann.get("storageclass.kubernetes.io/is-default-class") == "true",
        "parameter_keys": sorted(obj.get("parameters") or {}),
    }


def derive_csidriver(obj: dict) -> dict:
    spec = obj.get("spec") or {}
    return {"attach_required": spec.get("attachRequired"), "modes": list(spec.get("volumeLifecycleModes") or [])}


def derive_node_provider(provider_id: str) -> str:
    """Scheme of spec.providerID, e.g. "rke2" or "vsphere"; "none" when empty."""
    scheme, sep, _ = (provider_id or "").partition("://")
    return scheme if sep and scheme else "none"
