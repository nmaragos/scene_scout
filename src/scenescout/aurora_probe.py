"""Check Aurora initialization in a process that can fail independently."""
from PySide6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext

from .viewport import AURORA_RENDERER, _create_engine


def main():
    app = QGuiApplication([])
    context = QOpenGLContext()
    if not context.create():
        raise RuntimeError("Qt could not create an OpenGL context for Aurora.")
    surface = QOffscreenSurface()
    surface.setFormat(context.format())
    surface.create()
    if not surface.isValid() or not context.makeCurrent(surface):
        raise RuntimeError("Qt could not activate an OpenGL surface for Aurora.")
    try:
        engine = _create_engine(AURORA_RENDERER)
        del engine
    finally:
        context.doneCurrent()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
