from fastapi import APIRouter, Request

from inventory.web.deps import legacy_redirect

router = APIRouter()


@router.get("/inventory")
def inventory(request: Request):
    """The Inventory page is now the Clusters page; filters and format=csv are kept."""
    return legacy_redirect(request, "/clusters", "inventory")
