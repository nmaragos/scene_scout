"""Read the authored evidence for a selected composed change on demand."""
from bisect import bisect_left
from dataclasses import dataclass

from .usd_reader import _as_text


@dataclass(frozen=True)
class Opinion:
    layer: str
    spec_path: str
    value: str


@dataclass(frozen=True)
class LayerContributions:
    evidence: dict[str, tuple[str, ...]]
    highlight_paths: frozenset[str] = frozenset()

    @property
    def paths(self) -> frozenset[str]:
        return frozenset(self.evidence)

    def explain(self, path: str) -> str | None:
        evidence = self.evidence.get(path)
        return "\n".join(evidence) if evidence else None

    def summary(self, path: str) -> str | None:
        evidence = self.evidence.get(path)
        if not evidence:
            return None
        detail = next(
            (line for line in evidence if not line.startswith(("Prim spec ", "Property spec "))),
            evidence[0],
        )
        line = " ".join(detail.splitlines())
        return line if len(line) <= 180 else f"{line[:179].rstrip()}…"


def _describe_list_op(label: str, operation, item_text=str) -> str:
    if operation.isExplicit:
        items = ", ".join(item_text(item) for item in operation.explicitItems)
        return f"Authored {label} to {items}" if items else f"Authored empty {label} list"
    edits = []
    for verb, items in (
        ("adds", operation.addedItems), ("prepends", operation.prependedItems),
        ("appends", operation.appendedItems), ("removes", operation.deletedItems),
        ("reorders", operation.orderedItems),
    ):
        if items:
            edits.append(f"{verb} {', '.join(item_text(item) for item in items)}")
    return f"Authored {label} list {' · '.join(edits)}" if edits else f"Authored {label} list edit"


def _asset_arc_text(arc) -> str:
    target = str(arc.primPath)
    if arc.assetPath and target:
        return f"{arc.assetPath} at {target}"
    return arc.assetPath or target or "default prim"


def _authored_property_value(spec) -> str | None:
    if spec.HasInfo("connectionPaths"):
        return _describe_list_op("connection", spec.GetInfo("connectionPaths"))
    if spec.name == "material:binding" or spec.name.startswith("material:binding:"):
        if spec.HasInfo("targetPaths"):
            return _describe_list_op("material binding", spec.GetInfo("targetPaths"))
        if spec.HasInfo("bindMaterialAs"):
            return f"Authored material binding strength {_as_text(spec.GetInfo('bindMaterialAs'))}"
    if not spec.name.startswith("xformOp:") or not spec.HasInfo("default"):
        return None
    op = spec.name.split(":")[1]
    name = {
        "translate": "translation", "scale": "scale", "transform": "transform matrix", "orient": "orientation",
    }.get(op, spec.name)
    if op.startswith("rotate"):
        name = f"{op.removeprefix('rotate')} rotation"
    return f"Authored {name} default {_as_text(spec.GetInfo('default'))}"


def find_layer_contributions(stage, layer_identifier: str, *, cancelled=None) -> LayerContributions:
    """Find authored specs that participate in the supplied stage's current composition."""
    from pxr import Sdf, Usd, UsdGeom, UsdShade
    from .analysis import check_cancelled

    if not any(layer.identifier == layer_identifier for layer in stage.GetUsedLayers()):
        return LayerContributions({})

    evidence: dict[str, list[str]] = {}
    highlight_paths = set()
    geometry = []
    materials: dict[str, list[str]] = {}
    has_binding_opinions = False
    predicate = Usd.PrimIsActive & ~Usd.PrimIsAbstract
    for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies(predicate)):
        check_cancelled(cancelled)
        if prim.IsPseudoRoot() or not prim.IsDefined():
            continue
        path = str(prim.GetPath())
        if prim.IsA(UsdGeom.Gprim) or prim.IsA(UsdGeom.PointInstancer) or prim.IsA(UsdGeom.Subset):
            geometry.append(prim)
        specs = [
            spec for spec in prim.GetPrimStack()
            if spec.layer.identifier == layer_identifier
            and (
                spec.specifier != Sdf.SpecifierOver or spec.properties
                or set(spec.ListInfoKeys()) - {"specifier", "primChildren", "propertyChildren"}
            )
        ]
        if not specs:
            continue

        details = evidence.setdefault(path, [])
        if prim.IsA(UsdGeom.Imageable):
            highlight_paths.add(path)
        for spec in specs:
            details.append(f"Prim spec {spec.path}")
            for field, label in (("references", "reference"), ("payload", "payload")):
                if spec.HasInfo(field):
                    details.append(_describe_list_op(label, spec.GetInfo(field), _asset_arc_text))
            for prop in spec.properties:
                details.append(f"Property spec {prop.path}")
                value = _authored_property_value(prop)
                if value:
                    details.append(value)
            has_binding_opinions |= any(
                prop.name == "material:binding" or prop.name.startswith("material:binding:")
                for prop in spec.properties
            )

        material_prim = prim
        while material_prim and not material_prim.IsPseudoRoot():
            if material_prim.IsA(UsdShade.Material):
                materials.setdefault(str(material_prim.GetPath()), []).append(path)
                break
            material_prim = material_prim.GetParent()

    if geometry and (has_binding_opinions or materials):
        check_cancelled(cancelled)
        bound_materials, binding_relationships = UsdShade.MaterialBindingAPI.ComputeBoundMaterials(geometry)
        binding_evidence = {}
        material_evidence = {
            path: tuple(f"Bound material {path} has an authored contribution at {source}" for source in sources)
            for path, sources in materials.items()
        }
        for prim, material, relationship in zip(geometry, bound_materials, binding_relationships):
            check_cancelled(cancelled)
            path = str(prim.GetPath())
            if relationship and has_binding_opinions:
                relationship_path = relationship.GetPath()
                if relationship_path not in binding_evidence:
                    binding_evidence[relationship_path] = []
                    for spec in relationship.GetPropertyStack():
                        if spec.layer.identifier != layer_identifier:
                            continue
                        value = _authored_property_value(spec)
                        if value:
                            binding_evidence[relationship_path].extend(
                                (value, f"Resolved material binding has an authored opinion at {spec.path}")
                            )
                details = binding_evidence[relationship_path]
                if details:
                    evidence.setdefault(path, []).extend(details)
                    highlight_paths.add(path)
            if material:
                details = material_evidence.get(str(material.GetPath()))
                if details:
                    evidence.setdefault(path, []).extend(details)
                    highlight_paths.add(path)

    return LayerContributions(
        {path: tuple(dict.fromkeys(details)) for path, details in evidence.items()},
        frozenset(highlight_paths),
    )


def inspect_timed_attribute(stage, prim_path: str, attr_name: str, time_code: float) -> tuple[str, str, list[Opinion]]:
    """Evaluate an attribute and report authored evidence at numeric stage time."""
    from pxr import Sdf, Usd

    prim = stage.GetPrimAtPath(prim_path)
    if not prim:
        return "(absent)", "Prim is absent from the composed stage", []
    attr = prim.GetAttribute(attr_name)
    if not attr:
        return "(absent)", "Attribute is absent from the composed prim", []

    time = Usd.TimeCode(time_code)
    value = attr.Get(time)
    resolve = attr.GetResolveInfo(time)
    source = resolve.GetSource()
    clips = source == Usd.ResolveInfoSourceValueClips
    samples = source == Usd.ResolveInfoSourceTimeSamples
    spline = source == getattr(Usd, "ResolveInfoSourceSpline", None)
    default = source == Usd.ResolveInfoSourceDefault
    blocked = resolve.ValueIsBlocked()
    opinions = []
    winning_opinion_found = False

    for spec, offset in attr.GetPropertyStackWithLayerOffsets(time):
        layer = spec.layer
        path = spec.path
        if spec.HasInfo("default"):
            authored_default = spec.GetInfo("default")
            is_block = isinstance(authored_default, Sdf.ValueBlock)
            winning = not winning_opinion_found and (default or (blocked and is_block and not samples and not clips))
            winning_opinion_found |= winning
            label = "Winning default" if winning else "Authored default"
            display = "(blocked)" if is_block else _as_text(authored_default)
            opinions.append(Opinion(layer.identifier, str(path), f"{label} {display}"))
        if spline and spec.HasInfo("spline"):
            label = "Winning spline" if not winning_opinion_found else "Authored spline"
            opinions.append(Opinion(layer.identifier, str(path), label))
            winning_opinion_found = True

        authored_samples = sorted(
            (offset.offset + offset.scale * local, local) for local in layer.ListTimeSamplesForPath(path)
        )
        if not authored_samples:
            continue
        index = bisect_left(authored_samples, (time_code, float("-inf")))
        if index == 0 or (index < len(authored_samples) and authored_samples[index][0] == time_code):
            nearby = authored_samples[index:index + 1]
        else:
            nearby = authored_samples[index - 1:index + 1]
        winning = samples and not winning_opinion_found
        winning_opinion_found |= winning
        label = "Winning time samples" if winning else "Authored time samples"
        for sample_index, (stage_time, layer_time) in enumerate(nearby):
            sample = layer.QueryTimeSample(path, layer_time)
            is_block = isinstance(sample, Sdf.ValueBlock)
            if winning and sample_index == 0 and value is None and is_block:
                blocked = True
            display = "(blocked)" if is_block else _as_text(sample)
            opinions.append(Opinion(layer.identifier, str(path),
                                    f"{label} at stage t={stage_time:g} (layer t={layer_time:g}) · {display}"))

    if blocked and value is None:
        summary = f"Value is blocked at stage t={time_code:g}"
        if clips:
            summary += " · Value clips supply the resolved source"
        return "(blocked)", summary, opinions
    display = "(no value)" if value is None else _as_text(value)
    if value is None:
        summary = "No value resolves at this time"
    elif source == Usd.ResolveInfoSourceFallback:
        summary = "Value supplied by the schema fallback"
        if blocked:
            summary += " after an authored block"
    elif default:
        summary = "Value supplied by the strongest authored default"
    elif spline:
        summary = "Value evaluated from the composed spline"
    elif samples or clips:
        summary = _describe_sample_evaluation(attr, stage, time_code)
    else:
        summary = "Resolved composed value"
    if clips:
        summary += " · Value clips supply the resolved source"
    return display, f"Stage t={time_code:g} · {summary}", opinions


def _describe_sample_evaluation(attr, stage, time_code: float) -> str:
    from pxr import Usd

    bracket = attr.GetBracketingTimeSamples(time_code)
    if not bracket:
        return "Value evaluated from time samples"
    lower, upper = bracket
    if lower == upper:
        if lower == time_code:
            return f"Exact time sample at t={lower:g}"
        return f"Value held at the end sample at t={lower:g}"
    if stage.GetInterpolationType() == Usd.InterpolationTypeHeld:
        return f"Value held from t={lower:g}"
    return f"Between samples at t={lower:g} and t={upper:g} · Linear interpolation where the type supports it"


def inspect_dependency_opinions(stage, path, field):
    from .dependencies import authored_target_text, dependency_composition_errors, inspect_dependencies
    from .textures import TEXTURE_FIELD_PREFIX, inspect_texture_input, texture_status

    prim = stage.GetPrimAtPath(path)
    if not prim:
        return "Prim is absent from the composed stage", []
    if field.startswith(TEXTURE_FIELD_PREFIX):
        attr = prim.GetAttribute(field.removeprefix(TEXTURE_FIELD_PREFIX))
        assets = inspect_texture_input(stage, attr, attr.Get()) if attr else ()
        if not assets:
            return "No filesystem texture path checked for this input", []
        return "Composed texture paths · strongest authored default", [
            Opinion(asset.layer, asset.spec_path,
                    f"Authored texture · {asset.authored_path}\nResolver · {texture_status(asset)}")
            for asset in assets
        ]
    kind = field.split(".")[1]
    errors = dependency_composition_errors(prim)
    unloaded = prim.HasPayload() and not prim.IsLoaded()
    opinions = []
    for edit in inspect_dependencies(stage, prim):
        if kind not in (edit.kind, "composition"):
            continue
        lines = [f"Authored list edit · {edit.operation}"]
        for target in edit.targets:
            lines.append(f"Authored target · {authored_target_text(target)}")
            if edit.operation in ("delete", "reorder"):
                lines.append("List edit only")
                continue
            lines.append(f"Resolver · {target.resolver_status}")
            if target.identifier:
                lines.append(f"Resolver identifier · {target.identifier}")
            if target.resolved_path:
                lines.append(f"Resolved path · {target.resolved_path}")
            if target.composed_layer:
                lines.append(f"Effective dependency · {target.composed_layer} at {target.composed_prim_path}")
                if target.composed_offset or target.composed_scale != 1:
                    lines.append(f"Effective time mapping · stage offset {target.composed_offset:g}"
                                 f" · scale {target.composed_scale:g}")
            elif unloaded and edit.kind == "payload":
                lines.append("Effective dependency · intentionally unloaded")
            else:
                lines.append("Effective dependency · USD did not establish a contributing arc")
        opinions.append(Opinion(edit.layer, edit.spec_path, "\n".join(lines)))
    summary = "Authored dependency list edits · strongest first"
    if unloaded and kind in ("composition", "payload"):
        summary += "\nPayload intentionally unloaded · contents outside comparison coverage"
    if errors:
        summary += "\nUSD composition diagnostics\n" + "\n".join(error.message for error in errors)
    return summary, opinions


def change_evidence(stage, change, time=None):
    """Return the value, interpretation and authored opinions for a finding."""
    try:
        if change.time is not None and time is not None:
            name = change.field.partition(".")[2]
            return inspect_timed_attribute(stage, change.path, name, time)
        summary, opinions = inspect_opinions(stage, change.path, change.field)
        return None, summary, opinions
    except Exception as exc:
        return None, f"Unable to read authored evidence · {exc}", []


def inspect_opinions(stage, path: str, field: str) -> tuple[str, list[Opinion]]:
    from pxr import Sdf, Usd

    if field.startswith("dependencies."):
        return inspect_dependency_opinions(stage, path, field)
    category, _, name = field.partition(".")
    if path == "/":
        if category == "time_range":
            keys = ("startTimeCode", "endTimeCode", "startFrame", "endFrame")
            opinions = []
            for layer in (stage.GetSessionLayer(), stage.GetRootLayer()):
                if not layer:
                    continue
                spec = layer.pseudoRoot
                values = [f"{key} = {_as_text(spec.GetInfo(key))}" for key in keys if spec.HasInfo(key)]
                if values:
                    opinions.append(Opinion(layer.identifier, "/", "\n".join(values)))
            summary = ("Authored playback range · session and root layer opinions"
                       if stage.HasAuthoredTimeCodeRange() else "No complete authored playback range")
            return summary, opinions
        key = {"default_prim": "defaultPrim", "up_axis": "upAxis", "meters_per_unit": "metersPerUnit",
               "time_codes_per_second": "timeCodesPerSecond"}.get(category)
        opinions = []
        if key:
            for layer in stage.GetLayerStack():
                spec = layer.pseudoRoot
                if spec.HasInfo(key):
                    opinions.append(Opinion(layer.identifier, "/", _as_text(spec.GetInfo(key))))
        return "Authored stage metadata", opinions

    prim = stage.GetPrimAtPath(path)
    if not prim:
        return "Prim is absent from the composed stage", []

    def opinion(spec, value):
        return Opinion(spec.layer.identifier, str(spec.path), _as_text(value))

    if category == "connections":
        attr = prim.GetAttribute(name)
        if not attr:
            return "Attribute is absent", []
        return "Authored connection list edits · strongest first", [
            opinion(spec, _describe_list_op("connection", spec.GetInfo("connectionPaths")))
            for spec in attr.GetPropertyStack() if spec.HasInfo("connectionPaths")
        ]

    if category == "sample_times":
        return "Composed sample times could not be fully compared", []

    if category == "attributes":
        attr = prim.GetAttribute(name)
        if not attr:
            return "Attribute is absent", []
        resolve = attr.GetResolveInfo(Usd.TimeCode.Default())
        opinions = [opinion(spec, spec.default) for spec in attr.GetPropertyStack() if spec.HasInfo("default")]
        if resolve.ValueIsBlocked():
            return "Default value is blocked · authored defaults strongest first", opinions
        source = resolve.GetSource()
        if source == Usd.ResolveInfoSourceDefault:
            return "Winning default first · weaker authored defaults follow", opinions
        if source == Usd.ResolveInfoSourceFallback:
            return "Default supplied by the schema", opinions
        return "No resolved default value", opinions

    if category == "material_binding" or category == "relationships":
        rel = prim.GetRelationship(name or "material:binding")
        if not rel:
            return "Relationship is absent", []
        target_ops = [(spec, spec.GetInfo("targetPaths")) for spec in rel.GetPropertyStack()
                      if spec.HasInfo("targetPaths")]
        summary = "Authored target list operations · strongest first"
        if target_ops and target_ops[0][1].isExplicit:
            summary = "Strongest layer supplies an explicit target list."
        return summary, [opinion(spec, targets) for spec, targets in target_ops]

    stack = prim.GetPrimStack()
    if category == "variants":
        opinions = [opinion(spec, spec.variantSelections[name] or "<blocked>") for spec in stack
                    if name in spec.variantSelections]
        return "Authored variant selections · strongest first", opinions
    if category == "asset_info":
        opinions = [opinion(spec, spec.assetInfo[name]) for spec in stack if name in spec.assetInfo]
        return "Authored asset metadata · strongest first", opinions
    if category == "prim":
        opinions = [opinion(spec, spec.typeName or "(typeless)") for spec in stack if spec.specifier == Sdf.SpecifierDef]
        return "Contributing prim definitions · strongest first", opinions
    key = {"type_name": "typeName", "active": "active", "instance": "instanceable",
           "references": "references", "payloads": "payload"}.get(category)
    if key:
        return "Authored prim opinions · strongest first", [opinion(spec, spec.GetInfo(key)) for spec in stack
                                                           if spec.HasInfo(key)]
    return "Authored evidence is unavailable for this field", []
