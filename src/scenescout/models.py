"""Qt-independent immutable scene data models."""
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DependencyTarget:
    asset_path: str
    prim_path: str
    offset: float = 0.0
    scale: float = 1.0
    identifier: str = ""
    resolved_path: str = ""
    resolver_status: str = ""
    composed_layer: str = ""
    composed_prim_path: str = ""
    composed_order: int | None = None
    composed_offset: float = 0.0
    composed_scale: float = 1.0


@dataclass(frozen=True)
class DependencyOpinion:
    kind: str
    layer: str
    spec_path: str
    operation: str
    targets: tuple[DependencyTarget, ...]


@dataclass(frozen=True)
class CompositionError:
    kind: str
    message: str


@dataclass(frozen=True)
class TextureAsset:
    authored_path: str
    resolved_path: str
    layer: str
    spec_path: str
    udim: bool = False


@dataclass(frozen=True)
class PrimInfo:
    path: str
    name: str
    type_name: str
    active: bool
    loaded: bool
    instance: bool
    variants: dict[str, dict[str, Any]] = field(default_factory=dict)
    attributes: dict[str, str] = field(default_factory=dict)
    relationships: dict[str, list[str]] = field(default_factory=dict)
    references: list[str] = field(default_factory=list)
    payloads: list[str] = field(default_factory=list)
    material_binding: str = ""
    asset_info: dict[str, str] = field(default_factory=dict)
    sample_times: dict[str, list[float]] = field(default_factory=dict)
    dependencies: tuple[DependencyOpinion, ...] = ()
    composition_errors: tuple[CompositionError, ...] = ()
    texture_inputs: dict[str, tuple[TextureAsset, ...]] = field(default_factory=dict)
    attribute_fingerprints: dict[str, str] = field(default_factory=dict)
    asset_info_fingerprints: dict[str, str] = field(default_factory=dict)
    connections: dict[str, list[str]] = field(default_factory=dict)
    property_read_errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SceneSnapshot:
    source: str
    default_prim: str
    up_axis: str
    meters_per_unit: str
    time_codes_per_second: str
    layers: list[str]
    prims: dict[str, PrimInfo]
    notes: list[str] = field(default_factory=list)
    unloaded_payloads: list[str] = field(default_factory=list)
    loaded_payloads: list[str] = field(default_factory=list)
    time_range: tuple[float, float] | None = None
    sample_range: tuple[float, float] | None = None
    sample_index_errors: int = 0

    @property
    def prim_count(self) -> int:
        return len(self.prims)
