import pytest


Gf = pytest.importorskip("pxr.Gf")

from scenescout.framing import distance_to_fit


@pytest.mark.parametrize("aspect", [0.4, 1.0, 2.4])
@pytest.mark.parametrize("extent", [(12, 1, 4), (1, 10, 4), (2, 1, 16), (0.001, 0.002, 0.003)])
def test_perspective_fit_keeps_every_corner_inside(aspect, extent):
    center = Gf.Vec3d(13, 25, -7)
    half_extent = Gf.Vec3d(*extent) / 2
    bounds = Gf.Range3d(center - half_extent, center + half_extent)
    camera = Gf.Camera()
    camera.SetPerspectiveFromAspectRatioAndFieldOfView(aspect, 60, Gf.Camera.FOVVertical)
    orientation = Gf.Rotation(Gf.Vec3d.YAxis(), 32) * Gf.Rotation(Gf.Vec3d.XAxis(), 18)
    camera.transform = Gf.Matrix4d().SetRotate(orientation)
    frustum = camera.frustum
    distance = distance_to_fit(bounds, frustum, near=1.0)
    transform = camera.transform
    transform.SetTranslateOnly(center - frustum.ComputeViewDirection() * distance)
    camera.transform = transform
    camera.clippingRange = Gf.Range1f(1.0, 1000.0)
    projection = camera.frustum.ComputeViewMatrix() * camera.frustum.ComputeProjectionMatrix()

    for index in range(8):
        x, y, depth = projection.Transform(bounds.GetCorner(index))
        assert abs(x) < 0.9
        assert abs(y) < 0.9
        assert -1 < depth < 1
