from fastapi import APIRouter, Request

from inventory.web.deps import legacy_redirect

router = APIRouter()


@router.get("/namespaces")
def namespaces(request: Request):
    """Namespaces are now a section of the Workloads page; the query string and CSV links are kept."""
    return legacy_redirect(request, "/workloads", "namespaces")
