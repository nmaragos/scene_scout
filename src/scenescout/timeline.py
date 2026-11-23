"""Shared review time, expressed in the first available stage's time codes."""
import math

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel, QSlider,
    QStyle, QStyleOptionSlider, QWidget,
)


class TimeSlider(QSlider):
    scrubbed = Signal(float)

    def __init__(self):
        super().__init__(Qt.Horizontal)
        self.start = self.end = 0.0
        self.setToolTip("Drag by whole frames. Hold Shift and drag for subframes.")
        self.setAccessibleDescription(self.toolTip())
        self.valueChanged.connect(self._step)

    def set_interval(self, start, end):
        self.start, self.end = start, end
        span = end - start
        with QSignalBlocker(self):
            ticks = min(2_147_483_647, math.ceil(span * 1000))
            self.setRange(0, ticks)
            per_frame = max(1, min(ticks, round(ticks / span))) if span >= 1 else max(1, ticks)
            self.setSingleStep(per_frame)
            self.setPageStep(min(2_147_483_647, 10 * per_frame))

    def show_time(self, time):
        with QSignalBlocker(self):
            span = self.end - self.start
            self.setValue(round((time - self.start) / span * self.maximum()) if span else 0)

    def _step(self, position):
        if position == 0:
            self.scrubbed.emit(self.start)
        elif position == self.maximum():
            self.scrubbed.emit(self.end)
        else:
            time = self.start + position / self.maximum() * (self.end - self.start)
            self.scrubbed.emit(max(self.start, min(self.end, math.floor(time + 0.5))))

    def _track(self):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderGroove, self)
        handle = style.subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderHandle, self)
        return groove.left() + handle.width() / 2, max(1, groove.width() - handle.width()), option.upsideDown

    def _scrub_at_pointer(self, event):
        left, width, reverse = self._track()
        fraction = max(0.0, min(1.0, (event.position().x() - left) / width))
        value = self.start + (self.end - self.start) * (1 - fraction if reverse else fraction)
        if not (event.modifiers() & Qt.ShiftModifier) and self.start < value < self.end:
            value = math.floor(value + 0.5)
        self.scrubbed.emit(max(self.start, min(self.end, value)))

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        self.setSliderDown(True)
        self._scrub_at_pointer(event)
        event.accept()

    def mouseMoveEvent(self, event):
        if not self.isSliderDown() or not (event.buttons() & Qt.LeftButton):
            return super().mouseMoveEvent(event)
        self._scrub_at_pointer(event)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.isSliderDown():
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class TimeCodeBox(QDoubleSpinBox):
    def textFromValue(self, value):
        text = super().textFromValue(value)
        decimal = self.locale().decimalPoint()
        return text.rstrip("0").rstrip(decimal) if decimal in text else text


class ReviewTimeline(QWidget):
    timesChanged = Signal(object)
    alignmentChanged = Signal()

    def __init__(self):
        super().__init__()
        self._rates = {}
        self._range = None
        self._time = 0.0
        self._exact_b = None
        self._reference = "A"
        self._active_slot = "A"
        self.rate_b = 1.0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.time_fields = QGridLayout()
        self.time_fields.setContentsMargins(0, 0, 0, 0)
        self.stage_labels = {"A": QLabel("A"), "B": QLabel("B")}
        self.separator = QLabel("·")
        self.time_fields.addWidget(self.stage_labels["A"], 0, 0)
        self.time_fields.addWidget(self.separator, 0, 2)
        self.time_fields.addWidget(self.stage_labels["B"], 0, 3)
        layout.addLayout(self.time_fields)
        self.value = TimeCodeBox()
        self.value.setObjectName("reviewTimeCode")
        self.value.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.value.setStyleSheet(
            "QDoubleSpinBox#reviewTimeCode { background:#192331; border:1px solid #425465;"
            " border-radius:4px; padding:3px 6px; }"
            "QDoubleSpinBox#reviewTimeCode QLineEdit { border:0; padding:0; background:transparent; }"
        )
        self.value.setDecimals(9)
        self.value.setKeyboardTracking(False)
        self.value.setSingleStep(1)
        self.value.setAccessibleName("Review time code")
        self.value.valueChanged.connect(self._set_local_time)
        self.other_time = QLabel()
        self.time_fields.addWidget(self.value, 0, 1)
        self.time_fields.addWidget(self.other_time, 0, 4)
        self.slider = TimeSlider()
        self.slider.setAccessibleName("Scrub review time")
        self.slider.scrubbed.connect(self._set_local_time)
        layout.addWidget(self.slider, 1)
        self.alignment = QComboBox()
        self.alignment.addItem("Match by seconds")
        self.alignment.addItem("Match by frame number")
        self.alignment.setAccessibleName("A/B timeline alignment")
        self.alignment.setToolTip(
            "With A at 24 time codes/s and B at 30, Match by seconds compares A 24 with B 30. "
            "Match by frame number compares A 24 with B 24."
        )
        self.alignment.currentIndexChanged.connect(lambda _: self.alignmentChanged.emit())
        layout.addWidget(self.alignment)
        self.hide()

    @property
    def times(self):
        if self._range is None:
            return {}
        return {slot: self.local_time(slot) for slot in self._rates}

    def local_time(self, slot):
        if self._range is None or slot not in self._rates:
            return None
        if slot == "B":
            return self._time * self.rate_b if self._exact_b is None else self._exact_b
        return self._time

    def set_active_slot(self, slot):
        if slot == self._active_slot:
            return
        self._active_slot = slot
        if self._range is not None:
            self._configure_editor()
            self._update_readout()

    def focus_time_field(self):
        if self.value.isVisible():
            self.value.setFocus(Qt.OtherFocusReason)
            self.value.selectAll()

    @property
    def _editor_slot(self):
        return self._active_slot if self._active_slot in self._rates else self._reference

    def _set_local_time(self, value):
        if self._editor_slot == "B":
            self.set_time(value / self.rate_b, time_b=value)
        else:
            self.set_time(value)

    def configure(self, snapshots):
        previous = self.times
        reference = "A" if "A" in snapshots else "B"
        previous_rate = self._rates.get(self._reference)
        rates = {slot: float(scene.time_codes_per_second) for slot, scene in snapshots.items()}
        self._rates = rates
        self.rate_b = 1.0
        valid_rates = all(math.isfinite(rate) and rate > 0 for rate in rates.values())
        if valid_rates and self.alignment.currentIndex() == 0 and "A" in rates and "B" in rates:
            self.rate_b = rates["B"] / rates["A"]
        self.alignment.setEnabled(valid_rates)

        limits = []
        for slot, scene in snapshots.items():
            scale = self.rate_b if slot == "B" else 1.0
            for interval in (scene.time_range, scene.sample_range):
                if interval:
                    limits.extend(t / scale for t in interval)

        self._range = (min(limits), max(limits)) if limits else None
        self._reference = reference
        self.setVisible(bool(limits))
        if self._range is None:
            if previous:
                self.timesChanged.emit({})
            return

        start, end = self._range
        time = previous.get(reference, self._time if previous else start)
        if previous and reference not in previous and valid_rates and previous_rate and self.alignment.currentIndex() == 0:
            time = self._time * rates[reference] / previous_rate
        if not valid_rates:
            with QSignalBlocker(self.alignment):
                self.alignment.setCurrentIndex(1)
        self.alignment.setVisible(len(rates) == 2)
        self.other_time.setVisible(len(rates) == 2)
        self._configure_editor()
        self.set_time(time)

    def _configure_editor(self):
        slot = self._editor_slot
        scale = self.rate_b if slot == "B" else 1.0
        start, end = (t * scale for t in self._range)
        column = 1 if slot == "A" else 4
        self.time_fields.removeWidget(self.value)
        self.time_fields.removeWidget(self.other_time)
        self.time_fields.addWidget(self.value, 0, column)
        self.time_fields.addWidget(self.other_time, 0, 4 if slot == "A" else 1)
        self.separator.setVisible(len(self._rates) == 2)
        self.value.setAccessibleName(f"Stage {slot} review time code")
        self.value.setToolTip(f"Stage {slot} uses {self._rates[slot]:g} time codes per second.")
        with QSignalBlocker(self.value):
            self.value.setRange(start, end)
        metrics = self.value.fontMetrics()
        # Reserve space for the sign, integer digits and editable subframe digits.
        for name, field_column in (("A", 1), ("B", 4)):
            available = name in self._rates
            self.stage_labels[name].setVisible(available)
            width = 0
            if available:
                factor = self.rate_b if name == "B" else 1.0
                digits = max(len(str(abs(math.trunc(t * factor)))) for t in self._range)
                example = ("-" if start < 0 else "") + "8" * digits + "." + "8" * self.value.decimals()
                width = metrics.horizontalAdvance(example) + 22
            self.time_fields.setColumnMinimumWidth(field_column, width)
            if name == slot:
                self.value.setFixedWidth(width)
        self.value.setMinimumHeight(metrics.height() + 10)
        self.slider.set_interval(start, end)

    def set_time(self, time, *, time_b=None):
        if self._range is None:
            return
        start, end = self._range
        self._time = max(start, min(end, time))
        self._exact_b = time_b
        self._update_readout()
        self.timesChanged.emit(self.times)

    def _update_readout(self):
        slot = self._editor_slot
        local = self.local_time(slot)
        with QSignalBlocker(self.value):
            self.value.setValue(local)
        self.slider.show_time(local)
        other = "B" if slot == "A" else "A"
        if other in self._rates:
            self.other_time.setText(f"{self.local_time(other):.10g}")

