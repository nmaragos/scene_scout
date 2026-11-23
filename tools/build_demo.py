"""Generate browser assets from the bundled pump comparison."""

import json
from math import cos, pi, sin
from pathlib import Path

from pxr import Gf, UsdGeom, UsdShade
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPolygonF

from scenescout.diff import compare_scenes, field_label
from scenescout.provenance import change_evidence
from scenescout.usd_reader import open_stage, snapshot_stage


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples" / "review"
ASSETS = ROOT / "demo" / "assets"
CAMERA = Gf.Vec3d(3, 2.65, 6).GetNormalized()
RIGHT = Gf.Cross(Gf.Vec3d(0, 1, 0), CAMERA).GetNormalized()
UP = Gf.Cross(CAMERA, RIGHT)
LIGHT = Gf.Vec3d(-3, 6, 4).GetNormalized()


def geometry_faces(prim):
    if prim.IsA(UsdGeom.Cube):
        half = UsdGeom.Cube(prim).GetSizeAttr().Get() / 2
        points = [Gf.Vec3d(x, y, z) * half for x, y, z in (
            (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
            (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
        )]
        faces = ((3, 2, 1, 0), (4, 5, 6, 7), (0, 1, 5, 4),
                 (7, 6, 2, 3), (0, 4, 7, 3), (1, 2, 6, 5))
        return [[points[index] for index in face] for face in faces]

    cylinder = UsdGeom.Cylinder(prim)
    radius = cylinder.GetRadiusAttr().Get()
    half = cylinder.GetHeightAttr().Get() / 2
    segments = 48
    angles = [2 * pi * index / segments for index in range(segments)]
    lower = [Gf.Vec3d(radius * cos(angle), -half, radius * sin(angle)) for angle in angles]
    upper = [Gf.Vec3d(point[0], half, point[2]) for point in lower]
    sides = []
    for index in range(segments):
        following = (index + 1) % segments
        sides.append([lower[index], upper[index], upper[following], lower[following]])
    return [lower, upper[::-1], *sides]


def surface_color(prim):
    material, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
    if material:
        shader, _, _ = material.ComputeSurfaceSource()
        if shader:
            diffuse = shader.GetInput("diffuseColor").Get()
            if diffuse is not None:
                return diffuse
    display = UsdGeom.Gprim(prim).GetDisplayColorAttr().Get()
    return display[0] if display else (0.45, 0.48, 0.55)


def project(point):
    return QPointF(620 + 260 * Gf.Dot(point, RIGHT), 465 - 260 * Gf.Dot(point, UP))


def render_stage(stage, target):
    """Draw a fixed-camera preview of the fixture's cubes and Y-axis cylinder."""
    image = QImage(1200, 660, QImage.Format.Format_RGB32)
    gradient = QLinearGradient(0, 0, 0, image.height())
    gradient.setColorAt(0, QColor("#172739"))
    gradient.setColorAt(1, QColor("#0d141e"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.fillRect(image.rect(), gradient)
    painter.setPen(QColor("#2a3a4d"))
    for index in range(-7, 8):
        line = index / 2
        painter.drawLine(project(Gf.Vec3d(line, -0.09, -4)), project(Gf.Vec3d(line, -0.09, 4)))
        painter.drawLine(project(Gf.Vec3d(-4, -0.09, line)), project(Gf.Vec3d(4, -0.09, line)))

    transforms = UsdGeom.XformCache()
    polygons = []
    for prim in stage.Traverse():
        if not (prim.IsA(UsdGeom.Cube) or prim.IsA(UsdGeom.Cylinder)):
            continue
        transform = transforms.GetLocalToWorldTransform(prim)
        color = surface_color(prim)
        for face in geometry_faces(prim):
            points = [transform.Transform(point) for point in face]
            normal = Gf.Cross(points[1] - points[0], points[2] - points[0]).GetNormalized()
            if Gf.Dot(normal, CAMERA) <= 0:
                continue
            depth = sum(Gf.Dot(point, CAMERA) for point in points) / len(points)
            brightness = 0.35 + 0.65 * max(0, Gf.Dot(normal, LIGHT))
            fill = QColor.fromRgbF(*(min(1, (channel * brightness) ** (1 / 2.2)) for channel in color))
            polygons.append((depth, QPolygonF([project(point) for point in points]), fill))

    painter.setPen(Qt.PenStyle.NoPen)
    # Draw each face with a matching outline to cover subpixel seams in the cylinder.
    for _, polygon, fill in sorted(polygons, key=lambda item: item[0]):
        painter.setBrush(fill)
        painter.setPen(fill)
        painter.drawPolygon(polygon)
    painter.end()
    if not image.save(str(target)):
        raise OSError(f"Could not write {target.name}")


def evidence(stage, change, composed):
    _, summary, opinions = change_evidence(stage, change)
    return {
        "value": composed,
        "summary": summary,
        "opinions": [
            {"layer": Path(opinion.layer).name, "spec": opinion.spec_path, "value": opinion.value}
            for opinion in opinions
        ],
    }


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    before, after = (open_stage(str(SAMPLES / f"pump_{version}.usda")) for version in ("v012", "v013"))
    for stage, version in ((before, "v012"), (after, "v013")):
        render_stage(stage, ASSETS / f"pump-{version}.png")

    findings = []
    for change in compare_scenes(snapshot_stage(before), snapshot_stage(after)):
        # The binding relationship and the effective binding describe the same sample change.
        if change.field == "relationships.material:binding":
            continue
        category = "Other"
        if change.field == "material_binding":
            category = "Material"
        elif change.field.startswith("attributes.xformOp:"):
            category = "Transform"
        findings.append({
            "category": category,
            "label": field_label(change.field),
            "path": change.path,
            "stages": [evidence(before, change, change.before), evidence(after, change, change.after)],
        })
    findings.sort(key=lambda finding: (finding["category"], finding["path"]))
    data = json.dumps(findings, indent=2)
    (ASSETS / "findings.js").write_text(f"const pumpFindings = {data};\n", encoding="utf-8")


if __name__ == "__main__":
    main()
