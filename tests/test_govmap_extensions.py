"""Unit tests for read-only GovMap extension tools (network is fully mocked)."""
import io

from PIL import Image as PILImage
import govmap_extensions as ext


def _feature(g=2547, p=10):
    return {
        "type": "Feature",
        "properties": {"GUSH_NUM": g, "PARCEL": p, "LOCALITY_N": "Example"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[190000, 670000], [190070, 670000],
                             [190070, 670080], [190000, 670080], [190000, 670000]]],
        },
    }


def test_cadastral_exact_match(monkeypatch):
    monkeypatch.setattr(ext, "_http_get_json", lambda url, params: {"features": [_feature()]})
    result = ext.find_parcel(2547, 10)
    assert result["found"] is True
    assert result["gush"] == 2547 and result["helka"] == 10
    assert result["bbox_itm"] == [190000.0, 670000.0, 190070.0, 670080.0]
    assert "govmap.gov.il" in result["map_url"]
    assert 29 < result["center_wgs84"]["latitude"] < 34


def test_cadastral_does_not_guess_or_accept_wrong_parcel(monkeypatch):
    monkeypatch.setattr(ext, "_http_get_json", lambda url, params: {"features": [_feature(2547, 11)]})
    result = ext.find_parcel(2547, 10)
    assert result["found"] is False
    assert result["status"] == "not_found"


def test_cadastral_errors_not_mislabeled_not_found(monkeypatch):
    def bad(url, params):
        raise ValueError("test service error")
    monkeypatch.setattr(ext, "_http_get_json", bad)
    result = ext.find_parcel(2547, 10)
    assert result["status"] == "service_unavailable"
    assert len(result["errors"]) == 2


def test_reject_bad_parcel_numbers():
    for gush, helka in ((-1, 10), (2547, 0), (True, 10)):
        try:
            ext.find_parcel(gush, helka)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid parcel number was accepted")


def test_planning_queries_mark_failed_layer_as_unavailable(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            raise ValueError("service unavailable")
    class FakeClient:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, json, headers):
            assert len(json["point"]) == 2
            assert headers["Origin"] == "https://www.govmap.gov.il"
            return FakeResponse()
    monkeypatch.setattr(ext.httpx, "Client", FakeClient)
    result = ext.planning_layers(32.1, 34.8, 20, ["tabaot"])
    assert result["layers"]["tabaot"]["status"] == "unavailable"
    assert "warning" in result


def test_planning_for_missing_parcel_skips_queries(monkeypatch):
    monkeypatch.setattr(ext, "find_parcel", lambda g, p: {"found": False})
    result = ext.planning_layers_for_parcel(2547, 10)
    assert result["status"] == "parcel_unverified"


def test_map_uses_real_png_and_draws_verified_polygon(monkeypatch):
    from PIL import Image as PILImage
    img = PILImage.new("RGBA", (1080, 760), (255, 255, 255, 255))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")

    class Response:
        content = buffer.getvalue()
        def raise_for_status(self): pass

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, params, headers):
            assert params["layers"] == "PARCEL_ALL"
            assert params["srs"] == "EPSG:2039"
            return Response()

    monkeypatch.setattr(ext, "_http_get_json", lambda url, params: {"features": [_feature()]})
    monkeypatch.setattr(ext.httpx, "Client", Client)
    result = ext.wms_image_for_parcel(2547, 10)
    assert result["status"] == "ok"
    assert result["image"].startswith(b"\\x89PNG\\r\\n\\x1a\\n")


def test_map_fails_honestly_if_wms_not_png(monkeypatch):
    class Response:
        content = b"not a png"
        def raise_for_status(self): pass
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, params, headers): return Response()
    monkeypatch.setattr(ext, "_http_get_json", lambda url, params: {"features": [_feature()]})
    monkeypatch.setattr(ext.httpx, "Client", Client)
    assert ext.wms_image_for_parcel(2547, 10)["status"] == "wms_unavailable"
