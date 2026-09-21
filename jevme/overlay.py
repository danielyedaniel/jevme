"""The floating pill: live transcript, mic waveform, and what just happened.

A non-activating panel, so focus stays in whatever app you are talking to.
"""
from __future__ import annotations

import objc
from AppKit import (NSBackingStoreBuffered, NSBezierPath, NSButton, NSColor, NSFont, NSFontWeightMedium,
                    NSFontWeightSemibold, NSLineBreakByTruncatingHead, NSMakeRect, NSPanel, NSScreen,
                    NSTextField, NSView, NSVisualEffectBlendingModeBehindWindow, NSVisualEffectMaterialHUDWindow,
                    NSVisualEffectStateActive, NSVisualEffectView, NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowCollectionBehaviorStationary,
                    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel, NSBezelStyleInline,
                    NSImage, NSStatusWindowLevel, NSFloatingWindowLevel)
from Foundation import NSObject, NSTimer

WIDTH, HEIGHT = 520, 46
RADIUS = 14
BARS = 5


class WaveformView(NSView):
    """Five bars that bounce with the mic level."""

    def initWithFrame_(self, frame):
        self = objc.super(WaveformView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.levels = [0.0] * BARS
        self.target = 0.0
        self.active = True
        return self

    def setLevel_(self, level: float):
        self.target = float(level)

    def tick(self):
        import random
        for i in range(BARS):
            goal = self.target * (0.55 + 0.45 * random.random()) if self.active else 0.05
            self.levels[i] += (goal - self.levels[i]) * 0.45
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        b = self.bounds()
        w, h = b.size.width, b.size.height
        bar_w = 3.0
        gap = (w - BARS * bar_w) / (BARS - 1)
        color = NSColor.whiteColor() if self.active else NSColor.colorWithWhite_alpha_(1.0, 0.35)
        color.set()
        for i, lv in enumerate(self.levels):
            bh = max(3.0, (0.12 + 0.88 * lv) * h)
            x = i * (bar_w + gap)
            y = (h - bh) / 2
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(x, y, bar_w, bh), 1.5, 1.5).fill()


class Overlay(NSObject):
    def init(self):
        self = objc.super(Overlay, self).init()
        if self is None:
            return None
        self.toggle_handler = None
        self._status_clear_timer = None
        self._build()
        return self

    # ---------- build ----------

    @objc.python_method
    def _build(self):
        screen = NSScreen.mainScreen().visibleFrame()
        x = screen.origin.x + (screen.size.width - WIDTH) / 2
        y = screen.origin.y + screen.size.height - HEIGHT - 6
        rect = NSMakeRect(x, y, WIDTH, HEIGHT)
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(rect, style, NSBackingStoreBuffered, False)
        p = self.panel
        p.setLevel_(NSStatusWindowLevel)
        p.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary
                                 | NSWindowCollectionBehaviorStationary)
        p.setOpaque_(False)
        p.setBackgroundColor_(NSColor.clearColor())
        p.setHasShadow_(True)
        p.setMovableByWindowBackground_(True)
        p.setHidesOnDeactivate_(False)
        p.setFloatingPanel_(True)

        fx = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        fx.setMaterial_(NSVisualEffectMaterialHUDWindow)
        fx.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        fx.setState_(NSVisualEffectStateActive)
        fx.setWantsLayer_(True)
        fx.layer().setCornerRadius_(RADIUS)
        fx.layer().setMasksToBounds_(True)
        fx.layer().setBorderWidth_(0.5)
        fx.layer().setBorderColor_(NSColor.colorWithWhite_alpha_(1.0, 0.18).CGColor())
        p.setContentView_(fx)

        tint = NSView.alloc().initWithFrame_(fx.bounds())
        tint.setWantsLayer_(True)
        tint.layer().setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0.09, 0.09, 0.1, 0.72).CGColor())
        tint.setAutoresizingMask_(18)
        fx.addSubview_(tint)

        self.wave = WaveformView.alloc().initWithFrame_(NSMakeRect(16, 13, 22, 20))
        fx.addSubview_(self.wave)

        self.text = NSTextField.labelWithString_("")
        self.text.setFrame_(NSMakeRect(50, 12, WIDTH - 50 - 150, 22))
        self.text.setFont_(NSFont.systemFontOfSize_weight_(15, NSFontWeightMedium))
        self.text.setTextColor_(NSColor.whiteColor())
        self.text.setLineBreakMode_(NSLineBreakByTruncatingHead)
        self.text.setMaximumNumberOfLines_(1)
        fx.addSubview_(self.text)

        self.status = NSTextField.labelWithString_("")
        self.status.setFrame_(NSMakeRect(WIDTH - 150, 13, 112, 20))
        self.status.setFont_(NSFont.systemFontOfSize_weight_(12, NSFontWeightSemibold))
        self.status.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(0.45, 0.95, 0.6, 1.0))
        self.status.setAlignment_(2)  # right
        self.status.setLineBreakMode_(NSLineBreakByTruncatingHead)
        fx.addSubview_(self.status)

        self.button = NSButton.alloc().initWithFrame_(NSMakeRect(WIDTH - 34, 13, 20, 20))
        self.button.setBordered_(False)
        self.button.setBezelStyle_(NSBezelStyleInline)
        self.button.setTitle_("")
        self.button.setTarget_(self)
        self.button.setAction_("toggle:")
        self._set_button_icon(True)
        fx.addSubview_(self.button)

        self.anim = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 30.0, self, "animate:", None, True)
        p.orderFrontRegardless()

    @objc.python_method
    def _set_button_icon(self, listening: bool):
        name = "stop.fill" if listening else "play.fill"
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if img is not None:
            img.setTemplate_(True)
            self.button.setImage_(img)
            self.button.setContentTintColor_(NSColor.whiteColor())

    # ---------- api (main thread) ----------

    def setTranscript_(self, s: str):
        self.text.setStringValue_(s or "")

    def setLevel_(self, lv: float):
        self.wave.setLevel_(lv)

    def setListening_(self, on: bool):
        self.wave.active = bool(on)
        self._set_button_icon(bool(on))
        if not on:
            self.text.setStringValue_("paused")
            self.text.setTextColor_(NSColor.colorWithWhite_alpha_(1, 0.5))
        else:
            self.text.setTextColor_(NSColor.whiteColor())

    def setPreview_(self, s):
        """Dim hint of what is about to fire (or None)."""
        if s:
            self.status.setTextColor_(NSColor.colorWithWhite_alpha_(1.0, 0.55))
            self.status.setStringValue_("→ " + s)
            self._cancel_status_clear()

    def flashAction_(self, label: str):
        self.status.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(0.45, 0.95, 0.6, 1.0))
        self.status.setStringValue_("✓ " + label)
        self._clear_status_after(2.2)

    def flashError_(self, label: str):
        self.status.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(1.0, 0.45, 0.45, 1.0))
        self.status.setStringValue_("✗ " + label)
        self._clear_status_after(3.0)

    @objc.python_method
    def clearPreview(self):
        if str(self.status.stringValue()).startswith("→"):
            self.status.setStringValue_("")

    @objc.python_method
    def _clear_status_after(self, secs: float):
        self._cancel_status_clear()
        self._status_clear_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            secs, self, "clearStatus:", None, False)

    @objc.python_method
    def _cancel_status_clear(self):
        if self._status_clear_timer is not None:
            self._status_clear_timer.invalidate()
            self._status_clear_timer = None

    def clearStatus_(self, timer):
        self._status_clear_timer = None
        self.status.setStringValue_("")

    def animate_(self, timer):
        self.wave.tick()

    def toggle_(self, sender):
        if self.toggle_handler:
            self.toggle_handler()
