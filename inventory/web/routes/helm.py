from fastapi import APIRouter, Request

from inventory.web.deps import legacy_redirect

router = APIRouter()


@router.get("/helm")
def helm(request: Request):
    """Helm is now a section of the Platform page; the query string and CSV links are kept."""
    return legacy_redirect(request, "/platform", "charts")
