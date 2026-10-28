"""Scan with iPhone: Apple's Continuity Camera through the public AppKit mechanism.

Adapted from the author's own device-tested implementation (Continuity Camera is
delivered through the Services / responder-chain machinery):

1. An anchor NSView sits over the Qt "Scan with iPhone" button. Clicking the
   button pops up a contextual menu for that view with
   ``NSMenu.popUpContextMenu:withEvent:forView:``.
2. AppKit asks ``validRequestorForSendType:returnType:``; the anchor answers with
   the controller for PDF and photo types, so AppKit inserts its own items
   (Take Photo, Scan Documents, Add Sketch, per nearby iPhone or iPad). The app
   never lists or chooses devices itself.
3. AppKit looks for the receiver of the finished capture from the window's
   first responder, so the anchor holds the focus until the capture arrives
   (or the user picked none of Apple's items).
4. ``readSelectionFromPasteboard:`` reads the scan synchronously — the
   pasteboard is only valid during that call — and the bytes are saved
   unchanged (PDF for scans, the camera's JPEG/HEIC for photos).

Nothing is sent to the iPhone and nothing leaves the Mac except over Apple's
own local Continuity link.
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import Callable

IS_MAC = sys.platform == "darwin"

# Preference order: a scan stays a PDF, a photo stays the camera's own file.
ACCEPTED = [
    ("com.adobe.pdf", ".pdf"),
    ("public.heic", ".heic"),
    ("public.heif", ".heif"),
    ("public.jpeg", ".jpg"),
    ("public.png", ".png"),
    ("public.tiff", ".tiff"),
]
ACCEPTED_TYPES = {t for t, _ in ACCEPTED}

_controller = None
# The send/return types AppKit asked about (types only, never content) — diagnostics and tests.
REQUESTS: list[str] = []


def _objc():
    import objc
    from AppKit import NSApp, NSEvent, NSMenu, NSMenuItem, NSView
    from Foundation import NSObject, NSNotificationCenter, NSProcessInfo

    return objc, NSApp, NSEvent, NSMenu, NSMenuItem, NSView, NSObject, NSNotificationCenter, NSProcessInfo


def unique_name(folder: Path, stem: str, ext: str) -> Path:
    p = folder / f"{stem}{ext}"
    i = 2
    while p.exists():
        p = folder / f"{stem} {i}{ext}"
        i += 1
    return p


def save_payloads(payloads: list[tuple[str, bytes]], folder: Path) -> list[Path]:
    """Write captures byte-for-byte; scans are PDFs, photos keep their format."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y-%m-%d at %H.%M.%S")
    out = []
    for type_id, data in payloads:
        ext = dict(ACCEPTED).get(type_id, ".bin")
        stem = f"Scan {stamp}" if ext == ".pdf" else f"Photo {stamp}"
        path = unique_name(folder, stem, ext)
        tmp = path.with_name(f".{path.name}.part")
        tmp.write_bytes(data)
        tmp.replace(path)
        out.append(path)
    return out


def read_pasteboard(pboard) -> tuple[list[tuple[str, bytes]], list[str]]:
    """(payloads, offered type names) from a Continuity Camera pasteboard."""
    from Foundation import NSURL

    payloads: list[tuple[str, bytes]] = []
    offered: list[str] = []
    items = pboard.pasteboardItems() or []
    for item in items:
        types = [str(t) for t in (item.types() or [])]
        offered.append(", ".join(types))
        got = None
        # A promised content type (when given) first, then every accepted type, best first.
        promised = item.stringForType_("com.apple.pasteboard.promised-file-content-type")
        order = ([str(promised)] if promised and str(promised) in ACCEPTED_TYPES else []) + [t for t, _ in ACCEPTED]
        for t in order:
            if t in types:
                data = item.dataForType_(t)
                if data is not None and data.length() > 0:
                    got = (t, bytes(data))
                    break
        if got is None and "public.file-url" in types:
            url = NSURL.URLWithString_(item.stringForType_("public.file-url"))
            if url is not None and url.isFileURL():
                p = Path(str(url.path()))
                ext = p.suffix.lower()
                for t, e in ACCEPTED:
                    if e == ext or (ext == ".jpeg" and e == ".jpg"):
                        got = (t, p.read_bytes())
                        break
        if got is not None:
            payloads.append(got)
    if not payloads and not items:
        # Older single-representation pasteboards.
        for t, _e in ACCEPTED:
            data = pboard.dataForType_(t)
            if data is not None and data.length() > 0:
                payloads.append((t, bytes(data)))
                break
    return payloads, offered


def _build_classes():
    objc, NSApp, NSEvent, NSMenu, NSMenuItem, NSView, NSObject, NSNotificationCenter, NSProcessInfo = _objc()

    class ENGContinuityController(NSObject):
        def init(self):
            self = objc.super(ENGContinuityController, self).init()
            if self is None:
                return None
            self.on_receive = None
            self.on_choose_files = None
            self.on_help = None
            self.on_started = None
            self.anchor = None
            self.window = None
            self.previous = None
            self.chosen = False
            self.menu_open = False
            self.waiting = False
            self.open_menu = None
            return self

        # ---- NSServicesMenuRequestor
        @objc.typedSelector(b"Z@:@")
        def readSelectionFromPasteboard_(self, pboard):
            payloads, offered = read_pasteboard(pboard)
            self.waiting = False
            self.restoreFocus()
            if self.on_receive is not None:
                self.on_receive(payloads, offered)
            return bool(payloads)

        @objc.typedSelector(b"Z@:@@")
        def writeSelectionToPasteboard_types_(self, pboard, types):
            return False  # nothing is ever sent to the iPhone

        # ---- menu
        def chooseFiles_(self, sender):
            if self.on_choose_files:
                self.on_choose_files()

        def showHelp_(self, sender):
            if self.on_help:
                self.on_help()

        def menuWillOpen_(self, menu):
            self.menu_open = True
            self.open_menu = menu
            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                self, b"menuSentAction:", "NSMenuDidSendActionNotification", None)

        def menuDidClose_(self, menu):
            self.menu_open = False
            self.open_menu = None
            self.performSelector_withObject_afterDelay_(b"stopObserving:", None, 0.0)
            self.performSelector_withObject_afterDelay_(b"afterMenu:", None, 0.3)

        def stopObserving_(self, _):
            NSNotificationCenter.defaultCenter().removeObserver_name_object_(self, "NSMenuDidSendActionNotification", None)

        def menuSentAction_(self, note):
            item = (note.userInfo() or {}).get("MenuItem")
            if item is not None and item.target() is not self:
                self.chosen = True
                self.waiting = True
                if self.on_started:
                    self.on_started()

        def afterMenu_(self, _):
            if not self.chosen and not self.menu_open:
                self.restoreFocus()

        def giveUp_(self, _):
            if self.waiting:
                self.waiting = False
                self.restoreFocus()

        # ---- focus
        def focusAnchor(self):
            w = self.window
            if w is None or self.anchor is None:
                return
            fr = w.firstResponder()
            if fr is not self.anchor:
                self.previous = fr
            w.makeFirstResponder_(self.anchor)

        def restoreFocus(self):
            w = self.window
            if w is not None and w.firstResponder() is self.anchor:
                w.makeFirstResponder_(self.previous if self.previous is not w else None)
            self.previous = None

    class ENGContinuityAnchor(NSView):
        def validRequestorForSendType_returnType_(self, send_type, return_type):
            if len(REQUESTS) < 200:
                REQUESTS.append(f"send={send_type} return={return_type}")
            ctrl = _controller
            if ctrl is not None and (send_type is None or len(str(send_type)) == 0) and return_type is not None \
                    and str(return_type) in ACCEPTED_TYPES:
                return ctrl
            # Stop here: passing text requests on to Qt's view would add AutoFill / text
            # services for whatever text field had the focus before the menu opened.
            return None

        def acceptsFirstResponder(self):
            return True

        def canBecomeKeyView(self):
            return False

        def hitTest_(self, point):
            return None  # clicks belong to the Qt button underneath

        def isFlipped(self):
            return True

    return ENGContinuityController, ENGContinuityAnchor


_classes = None


class ContinuityCamera:
    """Python-side handle; one per application."""

    def __init__(self, on_receive: Callable[[list, list], None], on_choose_files: Callable[[], None],
                 on_help: Callable[[], None], on_started: Callable[[], None]):
        global _classes, _controller
        if _classes is None:
            _classes = _build_classes()
        ctrl_cls, self._anchor_cls = _classes
        self.ctrl = ctrl_cls.alloc().init()
        self.ctrl.on_receive = on_receive
        self.ctrl.on_choose_files = on_choose_files
        self.ctrl.on_help = on_help
        self.ctrl.on_started = on_started
        _controller = self.ctrl

    @property
    def waiting(self) -> bool:
        return bool(self.ctrl.waiting)

    def give_up(self):
        self.ctrl.giveUp_(None)

    def present(self, widget) -> bool:
        """Show Apple's Continuity Camera menu under a Qt widget (e.g. a button)."""
        objc, NSApp, NSEvent, NSMenu, NSMenuItem, NSView, NSObject, NSNotificationCenter, NSProcessInfo = _objc()
        from AppKit import NSEventTypeRightMouseDown, NSImage, NSMakeRect

        from .macos import _nswindow

        win = _nswindow(widget.window())
        if win is None or self.ctrl.menu_open:
            return False
        content = win.contentView()
        top_left = widget.mapTo(widget.window(), widget.rect().topLeft())
        w, h = widget.width(), widget.height()
        height = content.frame().size.height
        y = top_left.y() if content.isFlipped() else height - top_left.y() - h
        anchor = self.ctrl.anchor
        if anchor is None or anchor.window() is not win:
            if anchor is not None:
                anchor.removeFromSuperview()
            anchor = self._anchor_cls.alloc().initWithFrame_(NSMakeRect(top_left.x(), y, w, h))
            content.addSubview_(anchor)
            self.ctrl.anchor = anchor
        else:
            anchor.setFrame_(NSMakeRect(top_left.x(), y, w, h))
        self.ctrl.window = win

        menu = NSMenu.alloc().initWithTitle_("Scan with iPhone")
        menu.setDelegate_(self.ctrl)
        menu.setAutoenablesItems_(False)
        files = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Choose Files Instead…", b"chooseFiles:", "")
        files.setTarget_(self.ctrl)
        files.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_("folder", None))
        help_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("My iPhone Isn’t Listed…", b"showHelp:", "")
        help_item.setTarget_(self.ctrl)
        help_item.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_("questionmark.circle", None))
        menu.addItem_(files)
        menu.addItem_(help_item)

        corner = (0.0, float(h)) if anchor.isFlipped() else (0.0, 0.0)
        location = anchor.convertPoint_toView_(corner, None)
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
            NSEventTypeRightMouseDown, location, 0, NSProcessInfo.processInfo().systemUptime(), win.windowNumber(), None, 0, 1, 1.0)
        if event is None:
            return False
        self.ctrl.chosen = False
        self.ctrl.focusAnchor()
        NSMenu.popUpContextMenu_withEvent_forView_(menu, event, anchor)
        # Give up waiting after five minutes (the iPhone never reports Cancel).
        self.ctrl.performSelector_withObject_afterDelay_(b"giveUp:", None, 300.0)
        return True

    def deliver_for_test(self, pboard) -> bool:
        """Feed a pasteboard to the real receiver (tests and diagnostics only)."""
        return bool(self.ctrl.readSelectionFromPasteboard_(pboard))
