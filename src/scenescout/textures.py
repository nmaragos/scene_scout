"""Inspect composed filesystem texture references in USD shading inputs."""
import ntpath
import re

from .models import TextureAsset


TEXTURE_FIELD_PREFIX = "dependencies.texture."
SHADING_TYPES = frozenset({"Shader", "NodeGraph", "Material"})
_IMAGE_EXTENSIONS = frozenset({
    ".avif", ".bmp", ".dds", ".exr", ".gif", ".hdr", ".heic", ".heif",
    ".jpeg", ".jpg", ".jxl", ".ktx", ".ktx2", ".pic", ".png", ".psd", ".ptx",
    ".sgi", ".tga", ".tif", ".tiff", ".tx", ".webp",
})
# A single-letter drive prefix is a filesystem path on Windows.
_URI_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]+:")


def inspect_texture_input(stage, attr, value, udim_paths=None, *, connections=None) -> tuple[TextureAsset, ...]:
    from pxr import Ar, Sdf, Tf, UsdShade

    value_type = attr.GetTypeName()
    if value is None or value_type not in (Sdf.ValueTypeNames.Asset, Sdf.ValueTypeNames.AssetArray):
        return ()
    if connections is None:
        connections = attr.GetConnections()
    if connections:
        # The source input is inspected on its owning prim. The local value is a fallback.
        return ()

    shader = attr.GetPrim().GetAttribute("info:id")
    shader_id = str(shader.Get()) if shader else ""
    image_input = attr.GetName() == "inputs:file" and (
        shader_id == "UsdUVTexture" or shader_id.startswith(("ND_image_", "ND_tiledimage_", "ND_triplanarprojection_")))
    assets = (value,) if value_type == Sdf.ValueTypeNames.Asset else value
    candidates = []
    for asset in assets:
        path = asset.path
        if not path or _URI_SCHEME.match(path):
            continue
        suffix = ntpath.splitext(path.rstrip("]"))[1].lower()
        if image_input or suffix in _IMAGE_EXTENSIONS:
            candidates.append(asset)
    if not candidates:
        return ()

    source = next((spec for spec in attr.GetPropertyStack() if spec.HasInfo("default")), None)
    if source is None:
        return ()
    if udim_paths is None:
        udim_paths = {}
    results = []
    for asset in candidates:
        path = asset.path
        udim = UsdShade.UdimUtils.IsUdimIdentifier(path)
        resolved = asset.resolvedPath
        if udim:
            key = (source.layer.identifier, path)
            if key not in udim_paths:
                try:
                    with Ar.ResolverContextBinder(stage.GetPathResolverContext()):
                        udim_paths[key] = UsdShade.UdimUtils.ResolveUdimPath(path, source.layer)
                except Tf.ErrorException:
                    udim_paths[key] = None
            resolved = udim_paths[key]
            if resolved is None:
                continue
        results.append(TextureAsset(getattr(asset, "authoredPath", path), resolved,
                                    source.layer.identifier, str(source.path), udim))
    return tuple(results)


def texture_status(asset: TextureAsset) -> str:
    if asset.resolved_path:
        return f"Resolved · {asset.resolved_path}"
    return "No UDIM tiles resolved" if asset.udim else "Missing · resolver returned no path"


def texture_summary(assets: tuple[TextureAsset, ...]) -> str:
    return "\n".join(f"{asset.authored_path} · {texture_status(asset)}" for asset in assets)
