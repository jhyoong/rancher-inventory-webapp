"""Offline checks of the cluster list and table helpers on the Overview and Clusters pages."""


def test_cards_view_removed(admin_client):
    for path in ("/clusters", "/clusters?view=cards", "/", "/?view=cards"):
        body = admin_client.get(path).text
        assert "<th>Watch out</th>" in body, path
        assert "view-switch" not in body and "cluster-cards" not in body and ">Cards</a>" not in body, path
