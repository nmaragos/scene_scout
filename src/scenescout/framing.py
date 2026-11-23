"""Fit perspective viewport bounds without clipping at narrow aspect ratios."""
from pxr import Gf


def distance_to_fit(bounds, frustum, near):
    center = bounds.GetMidpoint()
    forward = frustum.ComputeViewDirection()
    up = frustum.ComputeUpVector()
    right = Gf.Cross(forward, up)
    # The frustum window gives the image extents at unit depth. Reserve
    # a margin on both axes and account for each corner's own depth.
    half_width, half_height = frustum.window.GetSize() / (2 * 1.12)
    distance = 0.0
    for index in range(8):
        offset = bounds.GetCorner(index) - center
        required_depth = max(
            abs(Gf.Dot(offset, right)) / half_width,
            abs(Gf.Dot(offset, up)) / half_height,
            near * 1.01,
        )
        distance = max(distance, required_depth - Gf.Dot(offset, forward))
    return distance
