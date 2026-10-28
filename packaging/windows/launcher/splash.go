// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright © 2026 ca-works

//go:build windows

package main

// A plain native splash: the app icon, the name and an indeterminate progress bar on a rounded card
// that follows the Windows light or dark app setting. It is the only splash; the application skips
// its own when it was started from here.

import (
	"syscall"
	"unsafe"
)

const (
	splashClass = "ENGDrawingViewer.Splash.1"
	splashW     = 360 // logical pixels (96 DPI); scaled to the monitor's DPI
	splashH     = 220
	timerID     = 1
)

type palette struct{ bg, border, text, secondary, track, accent uintptr }

var (
	lightColours = palette{rgb(0xFF, 0xFF, 0xFF), rgb(0xDD, 0xDF, 0xE4), rgb(0x1C, 0x1C, 0x1E), rgb(0x6E, 0x6E, 0x73), rgb(0xE5, 0xE7, 0xEB), rgb(0x1E, 0x5A, 0xDC)}
	darkColours  = palette{rgb(0x2B, 0x2B, 0x2E), rgb(0x3A, 0x3A, 0x3D), rgb(0xF2, 0xF2, 0xF7), rgb(0xA1, 0xA1, 0xA6), rgb(0x3A, 0x3A, 0x3D), rgb(0x4C, 0x8B, 0xF5)}
)

type splashWindow struct {
	hwnd   uintptr
	icon   uintptr
	dpi    uint32
	phase  int
	colour palette
}

var current *splashWindow

func appsUseDarkTheme() bool {
	var key syscall.Handle
	path := utf16Ptr(`Software\Microsoft\Windows\CurrentVersion\Themes\Personalize`)
	if syscall.RegOpenKeyEx(syscall.HKEY_CURRENT_USER, path, 0, syscall.KEY_READ, &key) != nil {
		return false
	}
	defer syscall.RegCloseKey(key)
	var value, kind uint32
	size := uint32(4)
	if syscall.RegQueryValueEx(key, utf16Ptr("AppsUseLightTheme"), nil, &kind, (*byte)(unsafe.Pointer(&value)), &size) != nil {
		return false
	}
	return kind == syscall.REG_DWORD && value == 0
}

func showSplash(exe string) *splashWindow {
	instance, _, _ := procGetModuleHandleW.Call(0)
	s := &splashWindow{colour: lightColours}
	if appsUseDarkTheme() {
		s.colour = darkColours
	}
	current = s
	wc := wndClassEx{
		Style:     csDropShadow,
		WndProc:   syscall.NewCallback(splashProc),
		Instance:  instance,
		ClassName: utf16Ptr(splashClass),
	}
	wc.Size = uint32(unsafe.Sizeof(wc))
	procRegisterClassExW.Call(uintptr(unsafe.Pointer(&wc)))

	// Centre on the monitor the pointer is on; that is where the user just clicked.
	var pt point
	procGetCursorPos.Call(uintptr(unsafe.Pointer(&pt)))
	mon, _, _ := procMonitorFromPoint.Call(uintptr(uint32(pt.X))|uintptr(uint32(pt.Y))<<32, monDefaultNear)
	mi := monitorInfo{}
	mi.Size = uint32(unsafe.Sizeof(mi))
	procGetMonitorInfoW.Call(mon, uintptr(unsafe.Pointer(&mi)))

	hwnd, _, _ := procCreateWindowExW.Call(wsExToolWindow, uintptr(unsafe.Pointer(utf16Ptr(splashClass))),
		uintptr(unsafe.Pointer(utf16Ptr(appName))), wsPopup, 0, 0, splashW, splashH, 0, 0, instance, 0)
	if hwnd == 0 {
		return nil
	}
	s.hwnd = hwnd
	s.dpi = 96
	if procGetDpiForWindow.Find() == nil {
		if d, _, _ := procGetDpiForWindow.Call(hwnd); d != 0 {
			s.dpi = uint32(d)
		}
	}
	w, h := s.scale(splashW), s.scale(splashH)
	x := mi.Work.Left + (mi.Work.Right-mi.Work.Left-w)/2
	y := mi.Work.Top + (mi.Work.Bottom-mi.Work.Top-h)/2 - s.scale(40)
	procSetWindowPos.Call(hwnd, 0, uintptr(x), uintptr(y), uintptr(w), uintptr(h), swpNoZOrder|swpNoActivate)
	if procDwmSetWindowAttribute.Find() == nil {
		pref := uint32(dwmwcpRound) // rounded corners on Windows 11; ignored on Windows 10
		procDwmSetWindowAttribute.Call(hwnd, dwmwaWindowCornerPreference, uintptr(unsafe.Pointer(&pref)), 4)
	}
	size := s.scale(64)
	s.icon, _, _ = procLoadImageW.Call(instance, 1, imageIcon, uintptr(size), uintptr(size), lrDefaultColor)
	procShowWindow.Call(hwnd, swShowNormal)
	procUpdateWindow.Call(hwnd)
	procSetTimer.Call(hwnd, timerID, 30, 0)
	return s
}

func (s *splashWindow) scale(v int32) int32 { return v * int32(s.dpi) / 96 }

func (s *splashWindow) close() {
	if s == nil || s.hwnd == 0 {
		return
	}
	procKillTimer.Call(s.hwnd, timerID)
	procDestroyWindow.Call(s.hwnd)
	if s.icon != 0 {
		procDestroyIcon.Call(s.icon)
	}
	s.hwnd = 0
	pumpMessages()
}

func splashProc(hwnd, message, wParam, lParam uintptr) uintptr {
	s := current
	switch message {
	case wmPaint:
		if s != nil {
			s.paint(hwnd)
			return 0
		}
	case wmTimer:
		if s != nil {
			s.phase = (s.phase + 1) % 120
			procInvalidateRect.Call(hwnd, 0, 0)
			return 0
		}
	case wmClose:
		return 0 // only the launcher closes the splash
	}
	r, _, _ := procDefWindowProcW.Call(hwnd, message, wParam, lParam)
	return r
}

func fill(hdc uintptr, r rect, colour uintptr) {
	brush, _, _ := procCreateSolidBrush.Call(colour)
	procFillRect.Call(hdc, uintptr(unsafe.Pointer(&r)), brush)
	procDeleteObject.Call(brush)
}

func (s *splashWindow) text(hdc uintptr, str string, r rect, height int32, weight int, colour uintptr) {
	font, _, _ := procCreateFontW.Call(uintptr(-s.scale(height)), 0, 0, 0, uintptr(weight), 0, 0, 0, 1, 0, 0,
		clearTypeQual, 0, uintptr(unsafe.Pointer(utf16Ptr("Segoe UI"))))
	old, _, _ := procSelectObject.Call(hdc, font)
	procSetTextColor.Call(hdc, colour)
	procSetBkMode.Call(hdc, transparentBk)
	u, _ := syscall.UTF16FromString(str)
	procDrawTextW.Call(hdc, uintptr(unsafe.Pointer(&u[0])), uintptr(len(u)-1), uintptr(unsafe.Pointer(&r)),
		dtCenter|dtVCenter|dtSingleLine|dtNoPrefix|dtEndEllipsis)
	procSelectObject.Call(hdc, old)
	procDeleteObject.Call(font)
}

func (s *splashWindow) paint(hwnd uintptr) {
	var ps paintStruct
	hdc, _, _ := procBeginPaint.Call(hwnd, uintptr(unsafe.Pointer(&ps)))
	defer procEndPaint.Call(hwnd, uintptr(unsafe.Pointer(&ps)))
	w, h := s.scale(splashW), s.scale(splashH)
	one := max(1, s.scale(1))
	fill(hdc, rect{0, 0, w, h}, s.colour.border)
	fill(hdc, rect{one, one, w - one, h - one}, s.colour.bg)
	if s.icon != 0 {
		size := s.scale(64)
		procDrawIconEx.Call(hdc, uintptr((w-size)/2), uintptr(s.scale(28)), s.icon, uintptr(size), uintptr(size), 0, 0, diNormal)
	}
	s.text(hdc, appName, rect{0, s.scale(104), w, s.scale(136)}, 20, fwSemiBold, s.colour.text)
	s.text(hdc, "Starting…", rect{0, s.scale(138), w, s.scale(160)}, 13, fwNormal, s.colour.secondary)

	// Indeterminate progress: a short segment sliding along a thin track.
	track := rect{s.scale(70), h - s.scale(36), w - s.scale(70), h - s.scale(33)}
	fill(hdc, track, s.colour.track)
	width := track.Right - track.Left
	seg := width / 3
	pos := int32(s.phase) * (width + seg) / 120
	left, right := max(track.Left, track.Left+pos-seg), min(track.Right, track.Left+pos)
	if right > left {
		fill(hdc, rect{left, track.Top, right, track.Bottom}, s.colour.accent)
	}
}
