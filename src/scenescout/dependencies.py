"""Read authored dependency list edits, resolver results and composed targets."""

from .models import CompositionError, DependencyOpinion, DependencyTarget


_ARC_FIELDS = (("reference", "references"), ("payload", "payload"))
_LIST_EDITS = (
    ("delete", "deletedItems"),
    ("add", "addedItems"),
    ("prepend", "prependedItems"),
    ("append", "appendedItems"),
    ("reorder", "orderedItems"),
)


def inspect_dependencies(stage, prim, *, stack=None) -> tuple[DependencyOpinion, ...]:
    """Inspect dependency opinions in the current prim stack and verify effective arcs with USD."""
    edits = []
    for spec in prim.GetPrimStack() if stack is None else stack:
        for kind, field in _ARC_FIELDS:
            if not spec.HasInfo(field):
                continue
            operation = spec.GetInfo(field)
            if operation.isExplicit:
                edits.append((kind, spec, "explicit", operation.explicitItems))
                continue
            for verb, items_field in _LIST_EDITS:
                items = getattr(operation, items_field)
                if items:
                    edits.append((kind, spec, verb, items))
    if not edits:
        return ()

    from pxr import Ar, Sdf

    composed = _composed_targets(prim)
    resolution_cache = {}
    opinions = []
    with Ar.ResolverContextBinder(stage.GetPathResolverContext()):
        resolver = Ar.GetResolver()
        for kind, spec, verb, items in edits:
            layer, path = spec.layer.identifier, str(spec.path)
            targets = []
            for item in items:
                asset_path = str(item.assetPath)
                key = (layer, asset_path)
                if key not in resolution_cache:
                    resolution_cache[key] = _resolve_asset(Sdf, resolver, spec.layer, asset_path)
                identifier, resolved_path, resolver_status = resolution_cache[key]
                effective = composed.get((kind, layer, path, item), ()) if verb not in {"delete", "reorder"} else ()
                target_layer, target_path, order, offset, scale = (
                    effective[0] if len(effective) == 1 else ("", "", None, 0.0, 1.0)
                )
                targets.append(DependencyTarget(
                    asset_path, str(item.primPath), item.layerOffset.offset, item.layerOffset.scale,
                    identifier, resolved_path, resolver_status, target_layer, target_path, order, offset, scale,
                ))
            opinions.append(DependencyOpinion(kind, layer, path, verb, tuple(targets)))
    return tuple(opinions)


def _composed_targets(prim):
    from pxr import Pcp, Usd

    query = Usd.PrimCompositionQuery(prim)
    criteria = Usd.PrimCompositionQuery.Filter()
    criteria.arcTypeFilter = Usd.PrimCompositionQuery.ArcTypeFilter.ReferenceOrPayload
    criteria.dependencyTypeFilter = Usd.PrimCompositionQuery.DependencyTypeFilter.Direct
    criteria.hasSpecsFilter = Usd.PrimCompositionQuery.HasSpecsFilter.HasSpecs
    query.filter = criteria

    composed = {}
    orders = {"reference": 0, "payload": 0}
    for arc in query.GetCompositionArcs():
        node = arc.GetTargetNode()
        if arc.IsImplicit() or not node.CanContributeSpecs():
            continue
        _, authored = arc.GetIntroducingListEditor()
        source, target = arc.GetIntroducingLayer(), arc.GetTargetLayer()
        if authored is None or not source or not target:
            continue
        kind = "reference" if arc.GetArcType() == Pcp.ArcTypeReference else "payload"
        key = (kind, source.identifier, str(arc.GetIntroducingPrimPath()), authored)
        offset = node.mapToRoot.Evaluate().timeOffset
        composed.setdefault(key, []).append(
            (target.identifier, str(arc.GetTargetPrimPath()), orders[kind], offset.offset, offset.scale)
        )
        orders[kind] += 1
    return composed


def _resolve_asset(sdf, resolver, layer, asset_path):
    if not asset_path:
        return "", "", "Internal target in the source layer stack"
    if sdf.Layer.IsAnonymousLayerIdentifier(asset_path):
        return asset_path, "", "Anonymous layer identifier"
    if asset_path.startswith("`"):
        return "", "", "Asset expression evaluated by composition"
    try:
        identifier = sdf.ComputeAssetPathRelativeToLayer(layer, asset_path)
        path, _ = sdf.Layer.SplitIdentifier(identifier)
        resolved = str(resolver.Resolve(path))
    except RuntimeError as exc:
        return "", "", f"Resolver query failed ({exc})"
    return identifier, resolved, "Resolved" if resolved else "Unresolved (resolver returned no path)"


def dependency_composition_errors(prim) -> tuple[CompositionError, ...]:
    """Use USD's composition failures, including arcs for which no target node exists."""
    from pxr import Pcp

    dependency_errors = {
        Pcp.ErrorType_ArcCycle,
        Pcp.ErrorType_ArcPermissionDenied,
        Pcp.ErrorType_InternalAssetPath,
        Pcp.ErrorType_InvalidAssetPath,
        Pcp.ErrorType_InvalidPrimPath,
        Pcp.ErrorType_InvalidReferenceOffset,
        Pcp.ErrorType_MutedAssetPath,
        Pcp.ErrorType_UnresolvedPrimPath,
    }
    index = prim.GetPrimIndex()
    return tuple(dict.fromkeys(
        CompositionError(str(error.errorType).removeprefix("Pcp.ErrorType_"), str(error).strip())
        for error in index.localErrors
        if error.errorType in dependency_errors and error.rootSite.path == index.rootNode.path
    ))


def authored_target_text(target: DependencyTarget) -> str:
    asset = target.asset_path or "Internal"
    path = target.prim_path or "default prim"
    text = f"{asset} · {path}"
    if target.offset or target.scale != 1:
        text += f" · offset {target.offset:g}, scale {target.scale:g}"
    return text


def authored_edit_text(opinion: DependencyOpinion) -> str:
    items = ", ".join(authored_target_text(item) for item in opinion.targets)
    return f"{opinion.operation.capitalize()} [{items}]"
