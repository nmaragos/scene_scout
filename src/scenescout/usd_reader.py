"""Read-only OpenUSD inspection. Imports pxr lazily for clear startup diagnostics."""
from pathlib import Path
from typing import NamedTuple

from .analysis import check_cancelled
from .comparison_value import value_fingerprint
from .models import PrimInfo, SceneSnapshot


class PrimHierarchyEntry(NamedTuple):
    path: str
    name: str
    type_name: str
    loaded: bool


def _as_text(value, limit=700):
    result = str(value)
    return result[:limit] + "…" if len(result) > limit else result


def _read_error(error):
    detail = f"{type(error).__name__}: {error}"
    return f"<unreadable: {_as_text(detail)}>"


def _list_op_items(value):
    if not value:
        return []
    output = []
    for section in ("explicitItems", "prependedItems", "appendedItems", "addedItems", "orderedItems"):
        for item in (getattr(value, section, None) or []):
            rendered = _as_text(item)
            if rendered not in output:
                output.append(rendered)
    return output


def inspect_stage(filename: str, load_payloads: bool = False) -> SceneSnapshot:
    return snapshot_stage(open_stage(filename, load_payloads))


def open_stage(filename: str, load_payloads: bool = False):
    try:
        from pxr import Usd
    except ImportError as exc:
        raise RuntimeError("OpenUSD Python bindings missing. Run uv sync and start with uv run scenescout.") from exc

    path = Path(filename).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    if path.suffix.lower() not in {".usd", ".usda", ".usdc", ".usdz"}:
        raise ValueError("Select a USD file (.usd, .usda, .usdc or .usdz).")

    stage = Usd.Stage.Open(str(path), load=Usd.Stage.LoadAll if load_payloads else Usd.Stage.LoadNone)
    if stage is None:
        raise ValueError(f"Unable to open USD stage: {path}")
    return stage


def stage_with_load_rules(stage, rules):
    """Compose a replacement stage without changing the stage in use by the UI."""
    from pxr import Usd

    with Usd.StageCacheContext(Usd.BlockStageCaches):
        replacement = Usd.Stage.Open(stage.GetRootLayer(), stage.GetSessionLayer(),
                                     stage.GetPathResolverContext(), Usd.Stage.LoadNone)
    if replacement is None:
        raise ValueError("Unable to compose the stage with the new payload settings.")
    replacement.SetLoadRules(rules)
    return replacement


def load_stage_payloads(stage, path: str | tuple[str, ...], load: bool = True):
    """Apply load rules for one or more branches before composing a replacement."""
    from pxr import Sdf, Usd

    paths = (path,) if isinstance(path, str) else path
    if "/" in paths:
        rules = Usd.StageLoadRules.LoadAll() if load else Usd.StageLoadRules.LoadNone()
    else:
        rules = stage.GetLoadRules()
        update_branch = rules.LoadWithDescendants if load else rules.Unload
        branches = {Sdf.Path(path) for path in paths}
        for branch in sorted(branches):
            if not any(parent in branches for parent in branch.GetPrefixes()[:-1]):
                update_branch(branch)
        rules.Minimize()
    return stage_with_load_rules(stage, rules)


def snapshot_stage(stage, *, hierarchy=None, progress=None, cancelled=None) -> SceneSnapshot:
    """Read a stage, optionally streaming lightweight prim rows before the full inspection."""
    from pxr import Usd, UsdGeom

    from .dependencies import dependency_composition_errors, inspect_dependencies
    from .textures import SHADING_TYPES, TEXTURE_FIELD_PREFIX, inspect_texture_input

    prims = {}
    browse_batch = []
    entries = []
    notes = []
    sample_range = None
    sample_index_errors = 0
    udim_paths = {}
    if progress:
        progress("Building hierarchy", 0, 0)
    check_cancelled(cancelled)
    payload_paths = set(stage.FindLoadable())
    payload_ancestors = {prefix for path in payload_paths for prefix in path.GetPrefixes()}
    predicate = Usd.PrimIsActive & ~Usd.PrimIsAbstract
    iterator = Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies(predicate))
    for prim in iterator:
        check_cancelled(cancelled)
        if prim.IsPseudoRoot():
            continue
        path = prim.GetPath()
        undefined_evidence = None
        if not prim.IsDefined() and path not in payload_ancestors and payload_paths.isdisjoint(path.GetPrefixes()):
            stack = prim.GetPrimStack()
            dependencies = inspect_dependencies(stage, prim, stack=stack)
            errors = dependency_composition_errors(prim)
            if not dependencies and not errors:
                continue
            undefined_evidence = (stack, dependencies, errors)
        row = PrimHierarchyEntry(str(path), prim.GetName(), prim.GetTypeName() or "(typeless)", prim.IsLoaded())
        entries.append((prim, row, undefined_evidence))
        if hierarchy:
            browse_batch.append(row)
            if len(entries) == 1 or len(browse_batch) == 128:
                hierarchy(tuple(browse_batch))
                browse_batch.clear()
                if progress:
                    progress("Building hierarchy", len(entries), 0)
    if hierarchy:
        hierarchy(tuple(browse_batch))
    total = len(entries)
    if progress:
        progress("Inspecting prims", 0, total)
    for index, (prim, row, undefined_evidence) in enumerate(entries, 1):
        check_cancelled(cancelled)
        variants = {}
        vsets = prim.GetVariantSets()
        for name in vsets.GetNames():
            v = vsets.GetVariantSet(name)
            variants[name] = {"selected": v.GetVariantSelection(), "choices": list(v.GetVariantNames())}
        attributes = {}
        attribute_fingerprints = {}
        sample_times = {}
        texture_inputs = {}
        connections = {}
        read_errors = {}
        shading_prim = row.type_name in SHADING_TYPES
        for attr in prim.GetAttributes():
            check_cancelled(cancelled)
            name = attr.GetName()
            targets = None
            try:
                targets = [str(path) for path in attr.GetConnections()]
                if targets:
                    connections[name] = targets
            except Exception as exc:
                read_errors[f"connections.{name}"] = _read_error(exc)
            try:
                times = attr.GetTimeSamples()
            except Exception as exc:
                read_errors[f"sample_times.{name}"] = _read_error(exc)
                sample_index_errors += 1
                if sample_index_errors == 1:
                    notes.append(f"Unable to read animation sample times for {attr.GetPath()}: {exc}")
                times = ()
            if times:
                sample_times[name] = times
                if sample_range is None:
                    sample_range = (times[0], times[-1])
                else:
                    sample_range = (min(sample_range[0], times[0]), max(sample_range[1], times[-1]))
            try:
                value = attr.Get(Usd.TimeCode.Default())
                type_name = str(attr.GetTypeName())
                display = f"{type_name}: {_as_text(value)}"
                attribute_fingerprints[name] = value_fingerprint((type_name, value))
            except Exception as exc:
                attributes[name] = read_errors[f"attributes.{name}"] = _read_error(exc)
                continue
            attributes[name] = display
            if shading_prim and name.startswith("inputs:") and targets is not None:
                try:
                    textures = inspect_texture_input(stage, attr, value, udim_paths, connections=targets)
                except Exception as exc:
                    read_errors[TEXTURE_FIELD_PREFIX + name] = _read_error(exc)
                    continue
                if textures:
                    texture_inputs[name] = textures
        relations = {}
        for rel in prim.GetRelationships():
            check_cancelled(cancelled)
            name = rel.GetName()
            try:
                relations[name] = [str(p) for p in rel.GetTargets()]
            except Exception as exc:
                read_errors[f"relationships.{name}"] = _read_error(exc)
        references, payloads = [], []
        if undefined_evidence is not None:
            stack, dependencies, errors = undefined_evidence
        else:
            stack = prim.GetPrimStack()
            dependencies = inspect_dependencies(stage, prim, stack=stack)
            errors = dependency_composition_errors(prim)
        for spec in stack:
            check_cancelled(cancelled)
            for category, dest in (("referenceList", references), ("payloadList", payloads)):
                for item in _list_op_items(getattr(spec, category, None)):
                    if item not in dest:
                        dest.append(item)
        binding = ", ".join(relations.get("material:binding", []))
        asset_info = prim.GetAssetInfo()
        prims[row.path] = PrimInfo(
            path=row.path, name=row.name, type_name=row.type_name,
            active=prim.IsActive(), loaded=row.loaded, instance=prim.IsInstance(),
            variants=variants, attributes=attributes, relationships=relations,
            references=references, payloads=payloads, material_binding=binding,
            asset_info={str(k): _as_text(v) for k, v in asset_info.items()},
            sample_times=sample_times, dependencies=dependencies, composition_errors=errors,
            texture_inputs=texture_inputs,
            attribute_fingerprints=attribute_fingerprints,
            asset_info_fingerprints={str(k): value_fingerprint(v) for k, v in asset_info.items()},
            connections=connections, property_read_errors=read_errors,
        )
        if progress and (index % 128 == 0 or index == total):
            progress("Inspecting prims", index, total)
    check_cancelled(cancelled)
    default_prim = stage.GetDefaultPrim()
    layers = [str(layer.identifier) for layer in stage.GetUsedLayers()]
    load_set = stage.GetLoadSet()
    unloaded_payloads = sorted(str(path) for path in payload_paths.difference(load_set))
    loaded_payloads = sorted(str(path) for path in payload_paths.intersection(load_set))
    if unloaded_payloads:
        notes.append("Unloaded payloads are outside this snapshot. Use the Prim Hierarchy context menu to load them.")
    time_range = None
    if stage.HasAuthoredTimeCodeRange():
        time_range = (stage.GetStartTimeCode(), stage.GetEndTimeCode())
    return SceneSnapshot(
        source=stage.GetRootLayer().realPath, default_prim=str(default_prim.GetPath()) if default_prim else "(none)",
        up_axis=str(UsdGeom.GetStageUpAxis(stage)),
        meters_per_unit=_as_text(UsdGeom.GetStageMetersPerUnit(stage)),
        time_codes_per_second=_as_text(stage.GetTimeCodesPerSecond()),
        layers=sorted(layers), prims=prims, notes=notes,
        unloaded_payloads=unloaded_payloads, loaded_payloads=loaded_payloads,
        time_range=time_range, sample_range=sample_range, sample_index_errors=sample_index_errors,
    )
