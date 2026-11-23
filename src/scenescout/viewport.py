"""Optional Hydra review viewport for a full OpenUSD installation."""
import sys
from base64 import b64encode
from collections import OrderedDict
from dataclasses import dataclass

from OpenGL import GL
from OpenGL.error import GLError
from PySide6.QtCore import QBuffer, QIODevice, QPointF, QProcess, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtOpenGL import QOpenGLFramebufferObject
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QLabel
from pxr import Gf, Glf, Sdf, Usd, UsdGeom, UsdImagingGL
from pxr.Usdviewq.freeCamera import FreeCamera

from .framing import distance_to_fit
from .review_document import THUMBNAIL_PAIR_LIMIT, ReviewThumbnails, ReviewView


STORM_RENDERER = "HdStormRendererPlugin"
AURORA_RENDERER = "HdAuroraRendererPlugin"
FINDING_CACHE_BYTES = 128 * 1024 * 1024
FINDING_CACHE_VIEWS = 16
THUMBNAIL_MAX_DIMENSION = 320
BLINK_KEYS = {Qt.Key_A: "A", Qt.Key_B: "B"}
_VIEWPORT_BADGE_STYLE = "background-color: #172532; color: #d7f1f3; border-radius: 4px; padding: 4px 8px;"
_WIPE_BADGE_STYLE = (
    "background-color: #172532; color: #d7f1f3; border-radius: 3px; font-size: 12px; font-weight: bold;"
)


@dataclass(frozen=True)
class _FindingView:
    camera: FreeCamera
    frame_bounds: Gf.Range3d | None
    frame_aspect: float | None


@dataclass(frozen=True)
class _CachedRender:
    view: _FindingView
    pixels: dict[str, bytes]
    unfinished: frozenset[str]
    size: int


def _create_engine(renderer):
    params = UsdImagingGL.Engine.Parameters()
    params.rendererPluginId = renderer
    engine = UsdImagingGL.Engine(params)
    if engine.GetCurrentRendererId() != renderer:
        raise RuntimeError(f"Hydra could not initialize {renderer}.")
    if renderer == AURORA_RENDERER:
        engine.SetRendererSetting("aurora:max_samples", 128)
    return engine


def _isolation_stage(source, path):
    # Add visibility opinions above the source session while retaining preview variants.
    session = Sdf.Layer.CreateAnonymous("SceneScout isolation.usda")
    original_session = source.GetSessionLayer()
    if original_session:
        session.subLayerPaths = [original_session.identifier]
    stage = Usd.Stage.OpenMasked(
        source.GetRootLayer(), session, source.GetPathResolverContext(),
        source.GetPopulationMask(), Usd.Stage.LoadNone,
    )
    stage.MuteAndUnmuteLayers(source.GetMutedLayers(), [])
    stage.SetLoadRules(source.GetLoadRules())
    stage.SetEditTarget(Usd.EditTarget(session))

    target = stage.GetPrimAtPath(path)
    # USD does not allow visibility overrides inside an instance proxy.
    while target and target.IsInstanceProxy():
        target = target.GetParent()
    target_path = target.GetPath() if target and target.IsActive() else Sdf.Path.emptyPath
    pending = list(stage.GetPseudoRoot().GetChildren())
    while pending:
        prim = pending.pop()
        prim_path = prim.GetPath()
        if prim_path == target_path:
            continue
        if target_path.HasPrefix(prim_path):
            pending.extend(prim.GetChildren())
        elif imageable := UsdGeom.Imageable(prim):
            imageable.MakeInvisible()
        else:
            pending.extend(prim.GetChildren())
    return stage


class ReviewViewport(QOpenGLWidget):
    primPicked = Signal(str, str)
    renderFailed = Signal(str)
    rendererChanged = Signal(str, str)
    thumbnailsReady = Signal(object, object)
    reviewCameraChanged = Signal()
    isolationChanged = Signal()

    def __init__(self):
        super().__init__()
        if STORM_RENDERER not in UsdImagingGL.Engine.GetRendererPlugins():
            raise RuntimeError("The OpenUSD installation has no Storm renderer.")
        self.setMinimumSize(280, 240)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("USD preview. Drag to orbit, middle-drag to pan, scroll to zoom.")
        self.setAccessibleDescription(
            "With two stages, drag the A/B divider. Use Left and Right to adjust it, "
            "Home to show only Stage B, or End to show only Stage A. "
            "Hold A or B for a temporary full-width view of that stage."
        )
        self.setMouseTracking(True)
        self.stages = {}
        self._isolated_stages = {}
        self.isolated_path = ""
        self._times = {}
        self._engines = {}
        self._buffers = {}
        self._replaced = set()
        self._bounds = Gf.BBox3d()
        self._waiting_for_bounds = set()
        self._frame_on_draw = False
        self._camera = FreeCamera(False)
        self._frame_bounds = None
        self._frame_aspect = None
        self._selected = ""
        self._layer_selection_slot = None
        self._layer_paths = ()
        self._comparison = None
        self._before_comparison = None
        self._wipe = 50
        self._dirty = True
        self._failed = False
        self._press = None
        self._previous = None
        self._dragging = False
        self._wipe_drag = None
        self._blink_keys = []
        self._wipe_labels = (QLabel("A", self), QLabel("B", self))
        for label in self._wipe_labels:
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet(_WIPE_BADGE_STYLE)
            label.setFixedSize(18, 18)
            label.hide()
        self._blink_label = QLabel(self)
        self._blink_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._blink_label.setStyleSheet(_VIEWPORT_BADGE_STYLE)
        self._blink_label.move(12, 12)
        self._blink_label.hide()
        self._render_label = QLabel("Rendering…", self)
        self._render_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._render_label.setStyleSheet(_VIEWPORT_BADGE_STYLE)
        self._render_label.adjustSize()
        self._render_label.hide()
        self._renderer = STORM_RENDERER
        self._renderer_probe = None
        self._probe_timeout = QTimer(self, singleShot=True, interval=15000)
        self._probe_timeout.timeout.connect(self._renderer_probe_timed_out)
        self._pending_render = set()
        self._advance = False
        self._progress = QTimer(self, singleShot=True, interval=16)
        self._progress.timeout.connect(self._advance_render)
        self._finding = None
        self._finding_changed = False
        self._shown_finding = None
        self._showing_cached_pixels = False
        self._finding_cache = OrderedDict()
        self._finding_cache_bytes = 0
        self._thumbnail_finding = None
        self._thumbnail_delay = QTimer(self, singleShot=True, interval=150)
        self._thumbnail_delay.timeout.connect(self.update)

    def set_renderer(self, renderer):
        self._cancel_renderer_probe()
        if renderer == STORM_RENDERER:
            self._use_renderer(STORM_RENDERER, "")
            return
        if renderer == self._renderer:
            return
        if renderer not in UsdImagingGL.Engine.GetRendererPlugins():
            self._use_renderer(STORM_RENDERER, "Aurora is unavailable in this OpenUSD runtime. Using Storm.")
            return
        # HdAurora can terminate its process when native initialization fails.
        probe = QProcess(self)
        self._renderer_probe = probe
        probe.finished.connect(lambda code, status: self._finish_renderer_probe(
            probe, code == 0 and status == QProcess.NormalExit))
        probe.errorOccurred.connect(lambda _: self._finish_renderer_probe(probe, False))
        self.rendererChanged.emit(AURORA_RENDERER, "Checking Aurora…")
        self._probe_timeout.start()
        probe.start(sys.executable, ["-B", "-m", "scenescout.aurora_probe"])

    def _finish_renderer_probe(self, probe, succeeded):
        if probe is not self._renderer_probe:
            return
        self._cancel_renderer_probe()
        if succeeded:
            self._use_renderer(AURORA_RENDERER, "Aurora is active. Select prims in the hierarchy.")
        else:
            self._use_renderer(STORM_RENDERER, "Aurora could not initialize. Using Storm.")

    def _renderer_probe_timed_out(self):
        if self._renderer_probe is not None:
            self._finish_renderer_probe(self._renderer_probe, False)

    def _cancel_renderer_probe(self):
        probe, self._renderer_probe = self._renderer_probe, None
        self._probe_timeout.stop()
        if probe is not None:
            probe.kill()
            probe.deleteLater()

    def _use_renderer(self, renderer, message):
        if renderer != self._renderer:
            self._renderer = renderer
            self._clear_finding_cache()
            self._replaced.update(self._buffers)
            self._pending_render.clear()
            self._render_label.hide()
            self._progress.stop()
            self._advance = False
            self._invalidate()
        self.rendererChanged.emit(renderer, message)

    def _advance_render(self):
        self._advance = True
        self.update()

    def set_stage(self, slot, stage, reframe=True):
        self.set_stages({slot: stage}, reframe=reframe)

    def set_stages(self, stages, reframe=True):
        replaced = {slot for slot, stage in stages.items() if reframe or self.stages.get(slot) is not stage}
        if not replaced:
            self.stage_changed()
            return
        self.clear_layer_comparison()
        self.stages.update(stages)
        self._replaced.update(replaced)
        if reframe:
            axis = UsdGeom.GetStageUpAxis(self.stages.get("A") or next(iter(stages.values())))
            self._camera = FreeCamera(axis == UsdGeom.Tokens.z)
            self._camera.Tumble(22, 18)
            self._frame_bounds = None
            self._waiting_for_bounds.update(replaced)
            self._frame_on_draw = True
        self.stage_changed()

    def stage_changed(self):
        self.clear_isolation()
        self._blink_keys.clear()
        self._blink_label.hide()
        self._thumbnail_finding = None
        self._finding = None
        self._clear_finding_cache()
        ranges = self._world_ranges()
        self._bounds = self._union_bounds(ranges)
        if self._comparison is None:
            for slot, extent in ranges.items():
                if slot in self._waiting_for_bounds and extent.GetSize().GetLength() > 0:
                    self._waiting_for_bounds.remove(slot)
                    self._frame_on_draw = True
        self._invalidate()

    def set_times(self, time_codes: dict[str, float]):
        times = dict(time_codes)
        if self._times != times:
            self._times = times
            self._bounds = self._union_bounds(self._world_ranges())
            self._invalidate()

    def _time_code(self, slot):
        source_slot = self._comparison[0] if self._comparison is not None else slot
        time = self._times.get(source_slot)
        return Usd.TimeCode(time) if time is not None else Usd.TimeCode.Default()

    def set_layer_comparison(self, slot, layer_identifier):
        stage = self.stages.get(slot)
        if stage is None:
            raise ValueError("Open an active stage before comparing layers.")
        layer = next(
            (layer for layer in stage.GetUsedLayers() if layer.identifier == layer_identifier),
            None,
        )
        if layer is None or stage.IsLayerMuted(layer_identifier):
            raise ValueError("The selected layer must be used and enabled in the active stage.")
        if layer in (stage.GetRootLayer(), stage.GetSessionLayer()):
            raise ValueError("The root and session layers cannot be muted in the comparison.")

        muted_stage = Usd.Stage.OpenMasked(
            stage.GetRootLayer(),
            stage.GetSessionLayer(),
            stage.GetPathResolverContext(),
            stage.GetPopulationMask(),
            Usd.Stage.LoadNone,
        )
        muted_stage.MuteAndUnmuteLayers([*stage.GetMutedLayers(), layer.identifier], [])
        muted_stage.SetLoadRules(stage.GetLoadRules())
        if self._comparison is None:
            camera = self._camera.clone()
            self._before_comparison = (self._camera, self._frame_bounds, self._frame_aspect, self._wipe)
            self._camera = camera
        self._comparison = (slot, muted_stage)
        self._wipe_drag = None
        self.unsetCursor()
        self._replaced.update(("A", "B"))
        self.stage_changed()

    def clear_layer_comparison(self):
        if self._comparison is not None:
            self._comparison = None
            self._camera, self._frame_bounds, self._frame_aspect, self._wipe = self._before_comparison
            self._before_comparison = None
            self._replaced.update(("A", "B"))
            self.stage_changed()

    def _display_stages(self):
        if self._comparison is None:
            return self._isolated_stages or self.stages
        slot, muted_stage = self._comparison
        return {"A": self.stages[slot], "B": muted_stage}

    def can_isolate(self, path):
        return bool(path and path != "/" and self._comparison is None
                    and any(stage.GetPrimAtPath(path) for stage in self.stages.values()))

    def isolate(self, path):
        if self.can_isolate(path):
            stages = {slot: _isolation_stage(stage, path) for slot, stage in self.stages.items()}
            self._set_isolation(path, stages)

    def clear_isolation(self):
        if self.isolated_path:
            self._set_isolation("", {})

    def _set_isolation(self, path, stages):
        self.isolated_path = path
        self._isolated_stages = stages
        self._thumbnail_finding = None
        self._finding = None
        self._clear_finding_cache()
        self._replaced.update(self.stages)
        self._bounds = self._union_bounds(self._world_ranges())
        self._invalidate()
        self.isolationChanged.emit()

    def _world_ranges(self, path=""):
        ranges = {}
        purposes = [UsdGeom.Tokens.default_, UsdGeom.Tokens.proxy, UsdGeom.Tokens.render]
        for slot, stage in self._display_stages().items():
            prim = stage.GetPrimAtPath(path) if path and path != "/" else stage.GetPseudoRoot()
            if prim:
                cache = UsdGeom.BBoxCache(self._time_code(slot), purposes, useExtentsHint=True)
                extent = cache.ComputeWorldBound(prim).ComputeAlignedRange()
                if not extent.IsEmpty():
                    ranges[slot] = extent
        return ranges

    @staticmethod
    def _union_bounds(ranges):
        result = Gf.Range3d()
        for extent in ranges.values():
            result.UnionWith(extent)
        return Gf.BBox3d(result)

    def frame(self, path="", *, refresh_thumbnails=False):
        bounds = self._union_bounds(self._world_ranges(path)) if path and path != "/" else self._bounds
        if not bounds.GetRange().IsEmpty() and bounds.GetRange().GetSize().GetLength() > 0:
            self._frame_on_draw = False
            self._frame_bounds = bounds.GetRange()
            self._frame_aspect = None
            self._camera.frameSelection(bounds, 1.0)
            self._invalidate()
            if refresh_thumbnails:
                self.reviewCameraChanged.emit()

    def capture_review_view(self):
        if not self._wipe_enabled() or not self.isValid() or self._failed:
            return None
        if self._frame_on_draw or self._finding_changed:
            self.repaint()
        if self._failed:
            return None
        self._frustum()
        camera = self._camera
        return ReviewView(tuple(camera.center), camera.dist, camera.rotTheta, camera.rotPhi,
                          camera.fov, self._wipe, self._times.get("A"), self._times.get("B"))

    def restore_review_view(self, view):
        if not self._wipe_enabled():
            return
        axis = UsdGeom.GetStageUpAxis(self.stages["A"])
        self._camera = FreeCamera(axis == UsdGeom.Tokens.z, fov=view.field_of_view)
        self._camera.center = Gf.Vec3d(*view.center)
        self._camera.dist = view.distance
        self._camera.rotTheta = view.azimuth
        self._camera.rotPhi = view.elevation
        self._camera_moved()
        self._finding_changed = False
        self.set_wipe(view.wipe)
        self._invalidate()

    def request_review_thumbnails(self, finding, *, debounce=False):
        self._thumbnail_delay.stop()
        self._thumbnail_finding = None if self.isolated_path else finding
        if self._thumbnail_finding is not None:
            if debounce:
                self._thumbnail_delay.start()
            else:
                self.update()

    def _capture_review_thumbnails(self):
        if (self._thumbnail_finding is None or self._thumbnail_delay.isActive()
                or self._pending_render or not self._wipe_enabled()
                or self._buffers.keys() != {"A", "B"}):
            return
        finding, self._thumbnail_finding = self._thumbnail_finding, None
        width, height = self._buffers["A"].width(), self._buffers["A"].height()
        scale = min(1.0, THUMBNAIL_MAX_DIMENSION / max(width, height))
        thumb_width, thumb_height = max(1, round(width * scale)), max(1, round(height * scale))
        images = []
        try:
            target = QOpenGLFramebufferObject(thumb_width, thumb_height)
            if not target.isValid():
                return
            GL.glDisable(GL.GL_SCISSOR_TEST)
            for slot in "AB":
                GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, self._buffers[slot].handle())
                GL.glReadBuffer(GL.GL_COLOR_ATTACHMENT0)
                GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, target.handle())
                GL.glBlitFramebuffer(0, 0, width, height, 0, 0, thumb_width, thumb_height,
                                     GL.GL_COLOR_BUFFER_BIT, GL.GL_LINEAR)
                # The blit leaves the full-size stage buffer bound for reading.
                # Bind the thumbnail to both targets before reading its pixels.
                if not target.bind():
                    return
                images.append(target.toImage())
        except GLError:
            return
        for quality in (75, 55, 35):
            encoded = []
            for image in images:
                buffer = QBuffer()
                if not buffer.open(QIODevice.WriteOnly) or not image.save(buffer, "JPEG", quality):
                    return
                encoded.append(b64encode(bytes(buffer.data())).decode("ascii"))
            pair = ReviewThumbnails(*encoded, self._times.get("A"), self._times.get("B"))
            if pair.size <= THUMBNAIL_PAIR_LIMIT:
                self.thumbnailsReady.emit(finding, pair)
                return

    def focus_finding(self, finding):
        if self.isolated_path:
            return
        if self._renderer != AURORA_RENDERER or not self._wipe_enabled():
            self._finding = finding if self._wipe_enabled() else None
            self.frame(finding.path)
            return
        if self._finding != finding:
            self._finding = finding
            self._finding_changed = True
            self._invalidate()

    def clear_finding(self):
        if self._finding is not None:
            self._finding = None
            self._finding_changed = True
            self.update()

    def _clear_finding_cache(self):
        self._finding_changed = False
        self._shown_finding = None
        self._showing_cached_pixels = False
        self._finding_cache.clear()
        self._finding_cache_bytes = 0

    def _finding_key(self, width, height):
        if self._renderer == AURORA_RENDERER and self._finding is not None and self._wipe_enabled():
            return self._finding, self._times.get("A"), self._times.get("B"), width, height

    def _save_finding_render(self):
        key, view = self._shown_finding
        if self._showing_cached_pixels and key in self._finding_cache:
            self._finding_cache.move_to_end(key)
            return
        width, height = key[-2:]
        size = 2 * width * height * 4  # RGBA8 color for each stage.
        if size > FINDING_CACHE_BYTES:
            return
        pixels = {}
        for slot, buffer in self._buffers.items():
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, buffer.handle())
            GL.glReadBuffer(GL.GL_COLOR_ATTACHMENT0)
            pixels[slot] = bytes(GL.glReadPixels(0, 0, width, height, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE))
        previous = self._finding_cache.pop(key, None)
        if previous is not None:
            self._finding_cache_bytes -= previous.size
        self._finding_cache[key] = _CachedRender(view, pixels, frozenset(self._pending_render), size)
        self._finding_cache_bytes += size
        while (self._finding_cache_bytes > FINDING_CACHE_BYTES
               or len(self._finding_cache) > FINDING_CACHE_VIEWS):
            _, removed = self._finding_cache.popitem(last=False)
            self._finding_cache_bytes -= removed.size

    def _restore_finding_render(self, key):
        cached = self._finding_cache.get(key)
        if cached is None:
            return False
        self._finding_cache.move_to_end(key)
        width, height = key[-2:]
        previous_texture = GL.glGetIntegerv(GL.GL_TEXTURE_BINDING_2D)
        try:
            for slot, pixels in cached.pixels.items():
                buffer = self._framebuffer(slot, width, height)
                GL.glBindTexture(GL.GL_TEXTURE_2D, buffer.texture())
                GL.glTexSubImage2D(GL.GL_TEXTURE_2D, 0, 0, 0, width, height,
                                   GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, pixels)
        finally:
            GL.glBindTexture(GL.GL_TEXTURE_2D, previous_texture)
        self._camera = cached.view.camera.clone()
        self._frame_bounds = cached.view.frame_bounds
        self._frame_aspect = cached.view.frame_aspect
        self._frame_on_draw = False
        self._dirty = False
        self._progress.stop()
        self._pending_render = set(cached.unfinished)
        self._shown_finding = (key, cached.view)
        self._showing_cached_pixels = True
        return True

    def _recall_finding(self, key):
        self._finding_changed = False
        try:
            if self._shown_finding is not None and self._shown_finding[0] != key:
                self._save_finding_render()
                self._shown_finding = None
                self._showing_cached_pixels = False
            if key is not None and self._restore_finding_render(key):
                return True
        except GLError:
            # A failed cache transfer should leave live Aurora rendering available.
            self._clear_finding_cache()
        if key is not None:
            self.frame(self._finding.path)
        return False

    def select(self, path):
        if self._finding is not None and self._finding.path != path:
            self.clear_finding()
        if self._selected != path or self._layer_paths:
            self._selected = path
            self._layer_selection_slot = None
            self._layer_paths = ()
            self._invalidate()

    def select_layer_paths(self, slot, paths):
        paths = tuple(paths)
        selection_slot = slot if paths else None
        if (selection_slot, paths) != (self._layer_selection_slot, self._layer_paths):
            if paths:
                self.clear_finding()
            self._layer_selection_slot = selection_slot
            self._layer_paths = paths
            if paths:
                self._selected = ""
            self._invalidate()

    def set_wipe(self, percent):
        percent = min(100, max(0, percent))
        if self._wipe != percent:
            self._wipe = percent
            self.update()

    def _wipe_enabled(self):
        return self._comparison is None and len(self.stages) == 2

    def _blink_stage(self):
        return BLINK_KEYS[self._blink_keys[-1]] if self._blink_keys else None

    def _update_blink_preview(self):
        slot = self._blink_stage()
        if slot is None:
            self._blink_label.hide()
        else:
            self._blink_label.setText(f"Stage {slot}")
            self._blink_label.adjustSize()
            self._blink_label.show()
        self.update()

    def _wipe_grip(self):
        seam = self.width() * self._wipe / 100
        return QPointF(min(self.width() - 24, max(24, seam)), self.height() - 28)

    def _over_wipe(self, point):
        if not self._wipe_enabled() or self._blink_keys:
            return False
        seam = self.width() * self._wipe / 100
        grip_offset = point - self._wipe_grip()
        return abs(point.x() - seam) <= 6 or grip_offset.manhattanLength() <= 16

    def _paint_wipe_grip(self):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            grip = self._wipe_grip()
            x, y = grip.x(), grip.y()
            painter.setPen(QPen(QColor("#9ce0ea"), 1))
            painter.setBrush(QColor("#172532"))
            painter.drawEllipse(grip, 11, 11)
            painter.drawLine(QPointF(x, y - 4), QPointF(x, y + 4))
            for direction in (-1, 1):
                outer = QPointF(x + 7 * direction, y)
                inner = x + 4 * direction
                painter.drawLine(QPointF(inner, y - 3), outer)
                painter.drawLine(outer, QPointF(inner, y + 3))
        finally:
            painter.end()

    def _update_wipe_labels(self, visible):
        grip = self._wipe_grip()
        for label, offset in zip(self._wipe_labels, (-15, 15)):
            if visible:
                label.move(round(grip.x() + offset - label.width() / 2), round(grip.y()) + 9)
            label.setVisible(visible)

    def _invalidate(self):
        self._dirty = True
        self.update()

    def initializeGL(self):
        self.context().aboutToBeDestroyed.connect(self.release)
        self._dirty = True

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(250, self._check_context)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_label.move(self.width() - self._render_label.width() - 12, 12)

    def _check_context(self):
        if self.isVisible() and not self.isValid() and not self._failed:
            self._failed = True
            self.renderFailed.emit("Qt could not create an OpenGL context.")

    def release(self):
        self._cancel_renderer_probe()
        self._progress.stop()
        self._thumbnail_delay.stop()
        self._pending_render.clear()
        self._render_label.hide()
        self._update_wipe_labels(False)
        self._thumbnail_finding = None
        self._advance = False
        self._clear_finding_cache()
        if self.isValid():
            self.makeCurrent()
            self._engines.clear()
            self._buffers.clear()
            self.doneCurrent()

    def _frustum(self):
        width = self.width() if self._comparison is None else self.width() / 2
        aspect = max(1, width) / max(1, self.height())
        self._camera.aspectRatio = aspect
        if self._frame_bounds is not None and self._frame_aspect != aspect:
            frustum = self._camera.computeGfCamera(self._bounds).frustum
            self._camera.dist = distance_to_fit(self._frame_bounds, frustum, FreeCamera.defaultNear)
            self._frame_aspect = aspect
        return self._camera.computeGfCamera(self._bounds, autoClip=True).frustum

    def _params(self, slot, highlight=False):
        params = UsdImagingGL.RenderParams()
        params.frame = self._time_code(slot)
        params.clearColor = Gf.Vec4f(0.09, 0.12, 0.16, 1)
        params.colorCorrectionMode = "sRGB"
        params.enableSceneMaterials = True
        params.enableSceneLights = False
        params.showProxy = True
        params.showRender = True
        params.highlight = highlight
        return params

    def _framebuffer(self, slot, width, height):
        buffer = self._buffers.get(slot)
        if buffer is None or buffer.width() != width or buffer.height() != height:
            buffer = QOpenGLFramebufferObject(width, height, QOpenGLFramebufferObject.CombinedDepthStencil)
            if not buffer.isValid():
                raise RuntimeError("OpenGL could not allocate a review framebuffer.")
            self._buffers[slot] = buffer
        return buffer

    def _render(self, slot, stage, width, height, frustum):
        buffer = self._framebuffer(slot, width, height)
        if not buffer.bind():
            raise RuntimeError("OpenGL could not bind the review framebuffer.")
        engine = self._engines.get(slot)
        if engine is None:
            engine = _create_engine(self._renderer)
            self._engines[slot] = engine
        engine.SetRenderBufferSize(Gf.Vec2i(width, height))
        engine.SetRenderViewport(Gf.Vec4d(0, 0, width, height))
        engine.SetCameraState(frustum.ComputeViewMatrix(), frustum.ComputeProjectionMatrix())
        paths = (self._selected,) if self._selected and self._selected != "/" else ()
        if self._comparison is None and slot == self._layer_selection_slot:
            paths = self._layer_paths
        selection = []
        for path in paths:
            prim = stage.GetPrimAtPath(path)
            if prim and not prim.IsPseudoRoot():
                selection.append(prim.GetPath())
        if self._renderer == STORM_RENDERER:
            engine.SetSelected(selection)
            engine.SetSelectionColor(Gf.Vec4f(0.4, 0.8, 1.0, 0.45))
        camera_light = Glf.SimpleLight()
        camera_light.position = Gf.Vec4f(*frustum.position, 1.0)
        engine.SetLightingState([camera_light], Glf.SimpleMaterial(), Gf.Vec4f(0.2, 0.2, 0.2, 1))
        GL.glDisable(GL.GL_SCISSOR_TEST)
        GL.glViewport(0, 0, width, height)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glClearColor(0.09, 0.12, 0.16, 1)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        engine.Render(stage.GetPseudoRoot(), self._params(
            slot, highlight=bool(selection) and self._renderer == STORM_RENDERER))
        if self._renderer == AURORA_RENDERER and not engine.IsConverged():
            self._pending_render.add(slot)
        else:
            self._pending_render.discard(slot)

    def paintGL(self):
        if self._failed:
            return
        try:
            ratio = self.devicePixelRatioF()
            width, height = round(self.width() * ratio), round(self.height() * ratio)
            if width <= 0 or height <= 0:
                return
            for slot in self._replaced:
                self._engines.pop(slot, None)
                self._buffers.pop(slot, None)
                self._pending_render.discard(slot)
            self._replaced.clear()
            stages = self._display_stages()
            comparing_layer = self._comparison is not None
            render_width = max(1, width // 2) if comparing_layer else width
            resized = any(b.width() != render_width or b.height() != height for b in self._buffers.values())
            render_all = self._dirty or resized
            advance, self._advance = self._advance, False
            finding_key = self._finding_key(render_width, height)
            if self._finding_changed:
                if self._recall_finding(finding_key):
                    render_all = advance = False
                else:
                    render_all = render_all or self._dirty
            if stages and (render_all or (advance and self._wipe_drag is None)):
                if self._frame_on_draw and not comparing_layer:
                    self._frame_on_draw = False
                    self.frame()
                frustum = self._frustum()
                for slot, stage in stages.items():
                    if render_all or slot in self._pending_render:
                        self._render(slot, stage, render_width, height, frustum)
                self._dirty = False
                self._showing_cached_pixels = False
                if render_all:
                    self._shown_finding = None if finding_key is None else (
                        finding_key, _FindingView(self._camera.clone(), self._frame_bounds, self._frame_aspect))

            self._capture_review_thumbnails()
            target = self.defaultFramebufferObject()
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, target)
            GL.glDisable(GL.GL_SCISSOR_TEST)
            GL.glClearColor(0.09, 0.12, 0.16, 1)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            both = len(self._buffers) == 2
            blink = self._blink_stage() if both and not comparing_layer else None
            split = width // 2 if comparing_layer else round(width * self._wipe / 100)
            if blink is not None:
                split = width if blink == "A" else 0
            for slot, buffer in self._buffers.items():
                left, right = ((0, split) if slot == "A" else (split, width)) if both else (0, width)
                if left < right:
                    GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, buffer.handle())
                    source_left, source_right = (0, render_width) if comparing_layer else (left, right)
                    GL.glBlitFramebuffer(source_left, 0, source_right, height, left, 0, right, height,
                                         GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, target)
            if both and blink is None:
                GL.glEnable(GL.GL_SCISSOR_TEST)
                GL.glScissor(min(split, width - 1), 0, 1, height)
                GL.glClearColor(0.48, 0.83, 0.90, 1)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT)
                GL.glDisable(GL.GL_SCISSOR_TEST)
                if not comparing_layer:
                    self._paint_wipe_grip()
        except Exception as exc:
            self._render_label.hide()
            self._update_wipe_labels(False)
            if self._renderer == AURORA_RENDERER:
                self._use_renderer(STORM_RENDERER, f"Aurora could not render. Using Storm. {exc}")
            else:
                self._failed = True
                self.renderFailed.emit(str(exc))
            return
        self._update_wipe_labels(both and blink is None and not comparing_layer)
        self._render_label.setVisible(bool(self._pending_render))
        if self._pending_render and self._wipe_drag is None and not self._progress.isActive():
            self._progress.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._over_wipe(event.position()):
            self._wipe_drag = (event.position().x(), self._wipe)
            self.setCursor(Qt.SizeHorCursor)
            event.accept()
            return
        self._press = self._previous = event.position()
        self._dragging = False

    def mouseMoveEvent(self, event):
        point = event.position()
        if self._wipe_drag is not None:
            start_x, start_wipe = self._wipe_drag
            width = max(1, self.width())
            if point.x() <= 0:
                self.set_wipe(0)
            elif point.x() >= width - 1:
                self.set_wipe(100)
            else:
                self.set_wipe(start_wipe + 100 * (point.x() - start_x) / width)
            event.accept()
            return
        if self._press is None:
            if self._over_wipe(point):
                self.setCursor(Qt.SizeHorCursor)
            else:
                self.unsetCursor()
            return
        if not self._dragging and (point - self._press).manhattanLength() < 4:
            return
        self._dragging = True
        delta = point - self._previous
        self._previous = point
        if event.buttons() & Qt.MiddleButton:
            scale = self._camera.ComputePixelsToWorldFactor(max(1, self.height()))
            self._camera.Truck(-delta.x() * scale, delta.y() * scale)
        elif event.buttons() & Qt.LeftButton:
            self._camera.Tumble(delta.x() * 0.4, delta.y() * 0.4)
        else:
            return
        self._camera_moved()
        self._invalidate()

    def _camera_moved(self):
        self._frame_bounds = None
        if self._comparison is None:
            self._waiting_for_bounds.clear()
            self._frame_on_draw = False
        self.reviewCameraChanged.emit()

    def mouseReleaseEvent(self, event):
        if self._wipe_drag is not None and event.button() == Qt.LeftButton:
            self._wipe_drag = None
            self.update()
            if not self._over_wipe(event.position()):
                self.unsetCursor()
            event.accept()
            return
        if self._press is not None and not self._dragging and event.button() == Qt.LeftButton:
            self._pick(event.position())
        self._press = None

    def leaveEvent(self, event):
        if self._wipe_drag is None:
            self.unsetCursor()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if self._wipe_enabled():
            key = event.key()
            if key in BLINK_KEYS and event.modifiers() == Qt.NoModifier and self._wipe_drag is None:
                if not event.isAutoRepeat() and key not in self._blink_keys:
                    self._blink_keys.append(key)
                    self.unsetCursor()
                    self._update_blink_preview()
                event.accept()
                return
            positions = {
                Qt.Key_Left: self._wipe - 1,
                Qt.Key_Right: self._wipe + 1,
                Qt.Key_Home: 0,
                Qt.Key_End: 100,
            }
            if key in positions:
                if not self._blink_keys:
                    self.set_wipe(positions[key])
                event.accept()
                return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        key = event.key()
        if key in self._blink_keys:
            if not event.isAutoRepeat():
                self._blink_keys.remove(key)
                self._update_blink_preview()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        if self._blink_keys:
            self._blink_keys.clear()
            self._update_blink_preview()
        super().focusOutEvent(event)

    def wheelEvent(self, event):
        self._camera_moved()
        self._camera.AdjustDistance(1.15 ** (-event.angleDelta().y() / 120))
        self._invalidate()

    def _pick(self, point):
        if self._failed or self._replaced or not self._engines or not self.isValid():
            return
        width, height = self.width(), self.height()
        x = point.x()
        if self._comparison is not None:
            width /= 2
            slot = "A" if x < width else "B"
            if slot == "B":
                x -= width
        else:
            slot = next(iter(self._engines))
            if len(self._engines) == 2:
                slot = self._blink_stage() or ("A" if x < width * self._wipe / 100 else "B")
        if self._renderer == AURORA_RENDERER and "primId" not in self._engines[slot].GetRendererAovs():
            return
        frustum = self._frustum().ComputeNarrowedFrustum(
            Gf.Vec2d(2 * x / width - 1, 1 - 2 * point.y() / height),
            Gf.Vec2d(2 / width, 2 / height),
        )
        self.makeCurrent()
        try:
            GL.glDepthMask(GL.GL_TRUE)
            stage = self._display_stages()[slot]
            result = self._engines[slot].TestIntersection(frustum.ComputeViewMatrix(), frustum.ComputeProjectionMatrix(),
                                                         stage.GetPseudoRoot(), self._params(slot))
        except Exception as exc:
            self.renderFailed.emit(f"Picking failed. {exc}")
            return
        finally:
            self.doneCurrent()
        path = result[2] if result and isinstance(result[2], Sdf.Path) else Sdf.Path.emptyPath
        source_slot = self._comparison[0] if self._comparison is not None else slot
        self.primPicked.emit(source_slot, str(path))
