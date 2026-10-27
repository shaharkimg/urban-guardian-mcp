"""Verified-data GovMap parcel search, government WMS map and planning layer tools.

No geometry is guessed. If the live government service does not return a parcel or
image, the tool reports that fact. GovMap WMS is not a screenshot of the portal UI.
"""
from __future__ import annotations

import io
import math
import os
from typing import Any
from urllib.parse import urlencode

import httpx

OPEN_GOVMAP_WFS = "https://open.govmap.gov.il/geoserver/opendata/wfs"
OPEN_GOVMAP_WMS = "https://open.govmap.gov.il/geoserver/opendata/wms"
GOVMAP_CATALOG_POINT = "https://www.govmap.gov.il/api/layers-catalog/entitiesByPoint"
GOVMAP_SITE = "https://www.govmap.gov.il/"
TIMEOUT = 15.0

# Layer identifiers are from the project's GovMap discovery documentation.
# They are NOT promises that the government's live service will return a feature.
PLANNING_LAYERS = {
    "tabaot": ("186", "תב״עות / TABAOT"),
    "planned_plots": ("11", "רצף מגרשים / RETZEFMIGRASHIM"),
    "plan_boundaries": ("357", "גבולות תכנון / RETZEFGVULOT"),
}


def _good_positive_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9999999:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _extract_gush_helka(props: dict) -> tuple[int | None, int | None]:
    def n(*names):
        for name in names:
            value = props.get(name)
            try:
                if value is not None and float(value).is_integer():
                    return int(value)
            except (TypeError, ValueError):
                pass
        return None

    return n("GUSH_NUM", "gush_num", "GUSH", "gush"), n(
        "PARCEL", "parcel", "HELKA", "helka", "PARCEL_NUM"
    )


def _coords_from_geometry(geometry: dict):
    """Unroll GeoJSON polygon/multipolygon coordinates as XY pairs."""
    kind = geometry.get("type")
    if kind not in ("Polygon", "MultiPolygon"):
        return []
    points = []
    def walk(node):
        if isinstance(node, (list, tuple)):
            if (len(node) >= 2 and isinstance(node[0], (int, float))
                    and isinstance(node[1], (int, float))):
                if math.isfinite(node[0]) and math.isfinite(node[1]):
                    points.append((float(node[0]), float(node[1])))
            else:
                for child in node:
                    walk(child)
    walk(geometry.get("coordinates", []))
    return points


def _transform_shape(geometry: dict, source: str = "EPSG:2039", target: str = "EPSG:4326") -> dict:
    from pyproj import Transformer
    transformer = Transformer.from_crs(source, target, always_xy=True)
    def walk(a):
        if len(a) >= 2 and isinstance(a[0], (float, int)) and isinstance(a[1], (float, int)):
            x, y = transformer.transform(float(a[0]), float(a[1]))
            return [round(x, 7), round(y, 7), *list(a[2:])]
        return [walk(v) for v in a]
    return {"type": geometry["type"], "coordinates": walk(geometry["coordinates"])}


def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def _http_get_json(url: str, params: dict) -> dict:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
        response = client.get(url, params=params, headers={"User-Agent": "UrbanGuardianGovMap/1.1"})
        response.raise_for_status()
        result = response.json()
    if not isinstance(result, dict) or "features" not in result or not isinstance(result["features"], list):
        raise ValueError("WFS did not return a GeoJSON FeatureCollection")
    return result


def _parcel_record(gush: int, helka: int, feature: dict, layer_name: str) -> dict:
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    if not isinstance(geom, dict) or not _coords_from_geometry(geom):
        raise ValueError("No parcel geometry in government response")
    points = _coords_from_geometry(geom)
    box = _bbox(points)
    # GovMap opendata WFS requested explicitly EPSG:2039; never silently accept
    # coordinates in another projection and misplace a parcel.
    if not (90000 <= box[0] <= 400000 and 300000 <= box[1] <= 850000
            and 90000 <= box[2] <= 400000 and 300000 <= box[3] <= 850000):
        raise ValueError("Unexpected geometry coordinates (not EPSG:2039)")
    x = (box[0] + box[2]) / 2
    y = (box[1] + box[3]) / 2
    from pyproj import Transformer
    lon, lat = Transformer.from_crs("EPSG:2039", "EPSG:4326", always_xy=True).transform(x, y)
    if not (29.0 <= lat <= 34 and 34 <= lon <= 37):
        raise ValueError("Parcel coordinates fall outside the expected region")
    map_url = GOVMAP_SITE + "?" + urlencode({
        "c": f"{x:.2f},{y:.2f}",
        "z": "10",
        "b": "2",
        "lay": "PARCEL_ALL,SUB_GUSH_ALL",
    })
    return {
        "found": True, "gush": gush, "helka": helka,
        "locality": props.get("LOCALITY_N"), "registered_area_m2": props.get("LEGAL_AREA"),
        "registration_status": props.get("STATUS_TEX"),
        "bbox_itm": [round(v, 2) for v in box],
        "center_wgs84": {"latitude": round(lat, 7), "longitude": round(lon, 7)},
        "coordinates_note": "center of the parcel bounding box, not a surveyed centroid",
        "geometry": geom, "geometry_crs": "EPSG:2039",
        "map_url": map_url,
        "source": {
            "authority": "Survey of Israel / GovMap opendata",
            "service": OPEN_GOVMAP_WFS,
            "layer": layer_name,
        },
        "legal_note": "Informational GIS, not a title deed or cadastral legal certification.",
    }


def find_parcel(gush: int, helka: int) -> dict:
    """Look up a specific block/parcel in the Survey of Israel WFS.

    Returns official polygon geometry, map link and source. Never estimates a match.
    """
    gush = _good_positive_int(gush, "gush")
    helka = _good_positive_int(helka, "helka")
    errors = []
    successes = 0
    # Try unprefixed and workspace-prefixed typeNames; deployments can differ.
    for typename in ("PARCEL_ALL", "opendata:PARCEL_ALL"):
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": typename, "outputFormat": "application/json",
            "srsName": "EPSG:2039",
            "count": 10,
            "CQL_FILTER": f"GUSH_NUM={gush} AND PARCEL={helka}",
        }
        try:
            data = _http_get_json(OPEN_GOVMAP_WFS, params)
            successes += 1
            matches = []
            for feature in data["features"]:
                props = feature.get("properties") or {}
                g, h = _extract_gush_helka(props)
                if g == gush and h == helka:
                    matches.append(feature)
            if matches:
                candidate = matches[0]
                result = _parcel_record(gush, helka, candidate, typename)
                if len(matches) > 1:
                    result["warning"] = "Multiple matches (possible sub-gush); verify against legal parcel record"
                    result["matches_count"] = len(matches)
                return result
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"{typename}: {str(exc)[:160]}")
    return {
        "found": False, "gush": gush, "helka": helka,
        "status": "not_found" if successes else "service_unavailable",
        "source": OPEN_GOVMAP_WFS,
        "errors": errors,
        "note": "No verified parcel polygon. Do not infer boundaries or coordinates.",
    }


def _checked_radius(radius_m: int) -> int:
    if isinstance(radius_m, bool) or not isinstance(radius_m, int) or not 1 <= radius_m <= 500:
        raise ValueError("radius_m must be an integer from 1 to 500")
    return radius_m


def planning_layers(latitude: float, longitude: float, radius_m: int = 20,
                    layers: list[str] | None = None) -> dict:
    """Query selected GovMap planning layers near a WGS84 point.

    Results are always per-layer with independent status and raw feature attributes.
    Empty results do NOT establish absence of planning constraints.
    """
    from pyproj import Transformer
    if not (29.0 <= latitude <= 34.0 and 34.0 <= longitude <= 37.0):
        raise ValueError("Coordinates outside the supported region")
    radius_m = _checked_radius(radius_m)
    selected = layers if layers is not None else list(PLANNING_LAYERS)
    if not selected or len(selected) > len(PLANNING_LAYERS) or any(k not in PLANNING_LAYERS for k in selected):
        raise ValueError("Unknown planning layer; supported: " + ", ".join(PLANNING_LAYERS))
    x, y = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform(longitude, latitude)
    out = {}
    for key in dict.fromkeys(selected):
        layer_id, label = PLANNING_LAYERS[key]
        request = {"point": [round(x, 3), round(y, 3)],
                   "layers": [{"layerId": layer_id}], "tolerance": radius_m}
        try:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
                resp = client.post(GOVMAP_CATALOG_POINT, json=request, headers={
                    "Origin": "https://www.govmap.gov.il",
                    "Referer": "https://www.govmap.gov.il/",
                    "Accept": "application/json",
                })
                resp.raise_for_status()
                content = resp.json()
            if not isinstance(content, (dict, list)):
                raise ValueError("Unexpected API response format")
            # Retain source structure (capped in size) rather than guessing its schema.
            import json
            data_as_text = json.dumps(content, ensure_ascii=False)
            if len(data_as_text) > 18000:
                out[key] = {"layer_id": layer_id, "label": label, "status": "partial",
                            "note": "Response too large; truncated; cannot verify complete result",
                            "response_excerpt": data_as_text[:18000]}
            else:
                out[key] = {"layer_id": layer_id, "label": label, "status": "response_received",
                            "raw_response": content}
        except (httpx.HTTPError, ValueError) as exc:
            out[key] = {"layer_id": layer_id, "label": label, "status": "unavailable",
                        "error": str(exc)[:250]}
    return {
        "location_wgs84": {"latitude": latitude, "longitude": longitude},
        "radius_m_requested": radius_m, "source": GOVMAP_CATALOG_POINT,
        "layers": out,
        "warning": "A missing/empty response does not prove no plans or restrictions exist. Verify in Mavat and planning records.",
        "coordinate_system_for_api": "EPSG:3857",
    }


def planning_layers_for_parcel(gush: int, helka: int, radius_m: int = 20,
                              layers: list[str] | None = None) -> dict:
    parcel = find_parcel(gush, helka)
    if not parcel.get("found"):
        return {"status": "parcel_unverified", "parcel": parcel,
                "note": "Planning query skipped; no verified parcel location."}
    center = parcel["center_wgs84"]
    result = planning_layers(center["latitude"], center["longitude"], radius_m, layers)
    result["parcel"] = {"gush": gush, "helka": helka, "map_url": parcel["map_url"]}
    result["coverage_warning"] = ("Point query at bounding-box center only. "
                                  "It does not prove absence of layers elsewhere on a large parcel.")
    return result


def wms_image_for_parcel(gush: int, helka: int, background: str = "transparent"):
    """Return GovMap WMS PNG bytes or an explicit failure.

    Government WMS cadastral extract, not a screenshot of the website.
    """
    from PIL import Image as PILImage
    from PIL import ImageDraw
    parcel = find_parcel(gush, helka)
    if not parcel.get("found"):
        return {"status": "parcel_unverified", "parcel": parcel}
    if background not in ("transparent", "white"):
        raise ValueError("background must be transparent or white")
    box = parcel["bbox_itm"]
    width, height = 1080, 760
    dx = max(box[2] - box[0], 70.0)
    dy = max(box[3] - box[1], 70.0)
    scale = max(dx / (width * .58), dy / (height * .58))
    center_x, center_y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    span_x, span_y = width * scale, height * scale
    bbox = [center_x - span_x / 2, center_y - span_y / 2,
            center_x + span_x / 2, center_y + span_y / 2]
    params = {
        "service": "WMS", "version": "1.1.1", "request": "GetMap",
        "layers": "PARCEL_ALL", "styles": "", "format": "image/png",
        "srs": "EPSG:2039", "bbox": ",".join(str(v) for v in bbox),
        "width": width, "height": height, "transparent": "TRUE",
    }
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
        response = client.get(OPEN_GOVMAP_WMS, params=params,
                              headers={"User-Agent": "UrbanGuardianGovMap/1.1"})
        response.raise_for_status()
    if not response.content.startswith(b"\x89PNG\r\n\x1a\n") or len(response.content) > 8000000:
        return {"status": "wms_unavailable", "source": OPEN_GOVMAP_WMS,
                "note": "Government WMS did not provide a valid PNG; no image generated.",
                "map_url": parcel["map_url"]}
    img = PILImage.open(io.BytesIO(response.content)).convert("RGBA")
    if img.size != (width, height):
        return {"status": "wms_unavailable", "note": "Unexpected image size", "map_url": parcel["map_url"]}
    if background == "white":
        canvas = PILImage.new("RGBA", (width, height), "#ffffff")
        canvas.alpha_composite(img)
        img = canvas
    draw = ImageDraw.Draw(img, "RGBA")
    def xy(point):
        px = (point[0] - bbox[0]) / (bbox[2] - bbox[0]) * width
        py = (bbox[3] - point[1]) / (bbox[3] - bbox[1]) * height
        return (px, py)
    geom = parcel["geometry"]
    polygons = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    for poly in polygons:
        for idx, ring in enumerate(poly):
            pts = [xy(pt) for pt in ring if len(pt) >= 2]
            if len(pts) > 2:
                if idx == 0:
                    draw.polygon(pts, fill=(240, 60, 55, 45))
                draw.line(pts + [pts[0]], fill=(200, 18, 18, 255), width=6)
    output = io.BytesIO()
    img.save(output, format="PNG")
    return {"status": "ok", "image": output.getvalue(), "map_url": parcel["map_url"],
            "source": OPEN_GOVMAP_WMS,
            "note": "Real official cadastral WMS raster with highlighted official parcel polygon. Not a screenshot of GovMap portal."}
