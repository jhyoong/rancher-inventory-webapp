"""Offline checks of the cluster list and table helpers on the Overview and Clusters pages."""


def test_cards_view_removed(admin_client):
    for path in ("/clusters", "/clusters?view=cards", "/", "/?view=cards"):
        body = admin_client.get(path).text
        assert "<th>Watch out</th>" in body, path
        assert "view-switch" not in body and "cluster-cards" not in body and ">Cards</a>" not in body, path


def test_special_network_tile_links_to_clusters(admin_client):
    assert 'href="/clusters?flag=special-network-config"' in admin_client.get("/").text
    assert 'href="/clusters?flag=special-network-config&amp;site=dev"' in admin_client.get("/?site=dev").text
