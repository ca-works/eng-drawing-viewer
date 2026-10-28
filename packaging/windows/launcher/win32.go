// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright © 2026 ca-works

//go:build windows

package main

// The few Win32 functions the launcher needs, called through the standard syscall package so the
// launcher has no third-party dependencies. Every DLL named here is a Windows system DLL; main()
// restricts DLL loading to System32 before the first of them is loaded.

import (
	"syscall"
	"unsafe"
)

var (
	kernel32 = syscall.NewLazyDLL("kernel32.dll")
	user32   = syscall.NewLazyDLL("user32.dll")
	gdi32    = syscall.NewLazyDLL("gdi32.dll")
	dwmapi   = syscall.NewLazyDLL("dwmapi.dll")

	procSetDefaultDllDirectories = kernel32.NewProc("SetDefaultDllDirectories")
	procSetDllDirectoryW         = kernel32.NewProc("SetDllDirectoryW")
	procCreateMutexW             = kernel32.NewProc("CreateMutexW")
	procReleaseMutex             = kernel32.NewProc("ReleaseMutex")
	procCreateEventW             = kernel32.NewProc("CreateEventW")
	procGetModuleHandleW         = kernel32.NewProc("GetModuleHandleW")

	procRegisterClassExW          = user32.NewProc("RegisterClassExW")
	procCreateWindowExW           = user32.NewProc("CreateWindowExW")
	procDefWindowProcW            = user32.NewProc("DefWindowProcW")
	procDestroyWindow             = user32.NewProc("DestroyWindow")
	procShowWindow                = user32.NewProc("ShowWindow")
	procUpdateWindow              = user32.NewProc("UpdateWindow")
	procInvalidateRect            = user32.NewProc("InvalidateRect")
	procBeginPaint                = user32.NewProc("BeginPaint")
	procEndPaint                  = user32.NewProc("EndPaint")
	procFillRect                  = user32.NewProc("FillRect")
	procDrawTextW                 = user32.NewProc("DrawTextW")
	procDrawIconEx                = user32.NewProc("DrawIconEx")
	procLoadImageW                = user32.NewProc("LoadImageW")
	procDestroyIcon               = user32.NewProc("DestroyIcon")
	procPeekMessageW              = user32.NewProc("PeekMessageW")
	procTranslateMessage          = user32.NewProc("TranslateMessage")
	procDispatchMessageW          = user32.NewProc("DispatchMessageW")
	procMsgWaitForMultipleObjects = user32.NewProc("MsgWaitForMultipleObjects")
	procSetTimer                  = user32.NewProc("SetTimer")
	procKillTimer                 = user32.NewProc("KillTimer")
	procFindWindowExW             = user32.NewProc("FindWindowExW")
	procSendMessageTimeoutW       = user32.NewProc("SendMessageTimeoutW")
	procGetWindowThreadProcessId  = user32.NewProc("GetWindowThreadProcessId")
	procAllowSetForegroundWindow  = user32.NewProc("AllowSetForegroundWindow")
	procMessageBoxW               = user32.NewProc("MessageBoxW")
	procGetCursorPos              = user32.NewProc("GetCursorPos")
	procMonitorFromPoint          = user32.NewProc("MonitorFromPoint")
	procGetMonitorInfoW           = user32.NewProc("GetMonitorInfoW")
	procGetDpiForWindow           = user32.NewProc("GetDpiForWindow")
	procSetWindowPos              = user32.NewProc("SetWindowPos")

	procCreateFontW      = gdi32.NewProc("CreateFontW")
	procCreateSolidBrush = gdi32.NewProc("CreateSolidBrush")
	procSelectObject     = gdi32.NewProc("SelectObject")
	procDeleteObject     = gdi32.NewProc("DeleteObject")
	procSetTextColor     = gdi32.NewProc("SetTextColor")
	procSetBkMode        = gdi32.NewProc("SetBkMode")

	procDwmSetWindowAttribute = dwmapi.NewProc("DwmSetWindowAttribute")
)

const (
	loadLibrarySearchSystem32 = 0x00000800

	errorAlreadyExists = 183
	waitObject0        = 0x00000000
	waitTimeout        = 0x00000102
	infinite           = 0xFFFFFFFF
	synchronize        = 0x00100000
	queryLimitedInfo   = 0x00001000
	qsAllInput         = 0x04FF

	wmDestroy   = 0x0002
	wmPaint     = 0x000F
	wmClose     = 0x0010
	wmTimer     = 0x0113
	wmCopyData  = 0x004A
	wmQuit      = 0x0012
	wmDpiChange = 0x02E0

	wsPopup         = 0x80000000
	wsExToolWindow  = 0x00000080
	wsExAppWindow   = 0x00040000
	csDropShadow    = 0x00020000
	swShowNormal    = 1
	pmRemove        = 0x0001
	smtoAbortIfHung = 0x0002
	imageIcon       = 1
	lrDefaultColor  = 0
	diNormal        = 0x0003

	dtCenter       = 0x0001
	dtVCenter      = 0x0004
	dtSingleLine   = 0x0020
	dtEndEllipsis  = 0x8000
	dtNoPrefix     = 0x0800
	transparentBk  = 1
	clearTypeQual  = 5
	fwSemiBold     = 600
	fwNormal       = 400
	monDefaultNear = 2

	swpNoZOrder   = 0x0004
	swpNoActivate = 0x0010

	mbOK            = 0x00000000
	mbIconError     = 0x00000010
	mbSetForeground = 0x00010000

	dwmwaWindowCornerPreference = 33
	dwmwcpRound                 = 2

	asfwAny = 0xFFFFFFFF
)

var hwndMessage = ^uintptr(2) // HWND_MESSAGE == (HWND)-3

type point struct{ X, Y int32 }

type rect struct{ Left, Top, Right, Bottom int32 }

type msg struct {
	Hwnd    uintptr
	Message uint32
	WParam  uintptr
	LParam  uintptr
	Time    uint32
	Pt      point
	Private uint32
}

type wndClassEx struct {
	Size       uint32
	Style      uint32
	WndProc    uintptr
	ClsExtra   int32
	WndExtra   int32
	Instance   uintptr
	Icon       uintptr
	Cursor     uintptr
	Background uintptr
	MenuName   *uint16
	ClassName  *uint16
	IconSm     uintptr
}

type paintStruct struct {
	Hdc         uintptr
	Erase       int32
	RcPaint     rect
	Restore     int32
	IncUpdate   int32
	RgbReserved [32]byte
}

type monitorInfo struct {
	Size    uint32
	Monitor rect
	Work    rect
	Flags   uint32
}

type copyDataStruct struct {
	Data  uintptr
	Bytes uint32
	Ptr   uintptr
}

func utf16Ptr(s string) *uint16 {
	p, err := syscall.UTF16PtrFromString(s)
	if err != nil {
		p, _ = syscall.UTF16PtrFromString("")
	}
	return p
}

func ptr[T any](v *T) uintptr { return uintptr(unsafe.Pointer(v)) }

func rgb(r, g, b byte) uintptr { return uintptr(r) | uintptr(g)<<8 | uintptr(b)<<16 }

func messageBox(text, title string) {
	procMessageBoxW.Call(0, uintptr(unsafe.Pointer(utf16Ptr(text))), uintptr(unsafe.Pointer(utf16Ptr(title))),
		mbOK|mbIconError|mbSetForeground)
}
