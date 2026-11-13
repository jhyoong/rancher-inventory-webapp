from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/inventory")
def inventory(request: Request):
    """The Inventory page is now the Clusters page; filters and format=csv are kept."""
    query = request.url.query
    return RedirectResponse("/clusters" + (f"?{query}" if query else ""), status_code=307)
