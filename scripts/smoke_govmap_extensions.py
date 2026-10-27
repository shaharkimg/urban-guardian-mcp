"""Run a small read-only live GovMap integration smoke test.

Print only statuses, not full government responses. Not a substitute for unit tests.
"""
from govmap_extensions import find_parcel, planning_layers, wms_image_for_parcel

p = find_parcel(2547, 10)
print("Parcel: ", {"status": p.get("status"), "found": p.get("found"),
                     "error_count": len(p.get("errors", [])), "source": p.get("source")})
if p.get("found"):
    print("Parcel center (WGS84):", p.get("center_wgs84"))
    m = wms_image_for_parcel(2547, 10)
    print("GovMap WMS map:", m.get("status"), len(m.get("image", b"")))
    center = p["center_wgs84"]
    planning = planning_layers(center["latitude"], center["longitude"], layers=["tabaot"])
    print("Planning layers:", {k: v["status"] for k, v in planning["layers"].items()})
