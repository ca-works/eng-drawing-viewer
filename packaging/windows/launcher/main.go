// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright © 2026 ca-works

//go:build windows

// Command launcher is "ENG Drawing Viewer.exe", the small native program a user starts. It
//
//   - shows a splash at once (the Python application takes a moment to start, longer while
//     antivirus software scans it the first time) and closes it when the main window is up;
//   - hands files to an already running copy instead of starting a second one;
//   - starts the application in the "app" folder next to it with absolute file paths and a
//     clean environment, and records failures in a small log file.
//
// It never touches the network and writes nothing except its log.
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	appName = "ENG Drawing Viewer"
	// The application side of these names lives in src/eng_viewer/platform/windows.py.
	receiverClass = "ENGDrawingViewer.Receiver.1"
	startupMutex  = `Local\ENGDrawingViewer.Startup.1`
	copyDataMagic = 0x454E4731 // "ENG1"
	coreRelPath   = `app\ENGDrawingViewer.exe`
)

// Environment variables that could make the bundled interpreter or Qt load code from elsewhere.
var unsafeEnv = []string{"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONUSERBASE",
	"PYTHONEXECUTABLE", "PYTHONSAFEPATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH",
	"ENG_VIEWER_READY_EVENT", "ENG_VIEWER_LAUNCHER"}

func init() {
	// Before any other DLL is loaded: look for DLLs only in System32, never in the current folder or
	// the folder of a downloaded file.
	procSetDefaultDllDirectories.Call(loadLibrarySearchSystem32)
	procSetDllDirectoryW.Call(uintptr(unsafe.Pointer(utf16Ptr(""))))
	// The splash window and its message loop must stay on one OS thread.
	runtime.LockOSThread()
}

func main() {
	os.Exit(run())
}

func run() int {
	logf("start %s, %d argument(s)", version, len(os.Args)-1)
	self, err := os.Executable()
	if err != nil {
		fail("Could not find the application folder.", err)
		return 1
	}
	core := filepath.Join(filepath.Dir(self), coreRelPath)
	if _, err := os.Stat(core); err != nil {
		fail("The application files are missing. Please extract the whole ZIP file (or reinstall) and try again.", err)
		return 1
	}
	args := absoluteArgs(os.Args[1:])
	smoke := os.Getenv("ENG_VIEWER_SMOKE_TEST") == "1"

	if !smoke {
		if forward(args) {
			logf("handed %d path(s) to the running copy", len(args))
			return 0
		}
		// Another launcher may be starting the application right now: wait for it to finish, then
		// hand over to the copy it started.
		mutex, existed := createMutex(startupMutex)
		if existed {
			syscall.WaitForSingleObject(syscall.Handle(mutex), 90_000)
			if forward(args) {
				logf("handed %d path(s) to the copy another launcher started", len(args))
				return 0
			}
		}
		defer func() {
			procReleaseMutex.Call(mutex)
			syscall.CloseHandle(syscall.Handle(mutex))
		}()
	}
	return start(self, core, args, smoke)
}

// absoluteArgs turns file arguments into absolute paths, because the application starts in its own
// folder, not in the folder the user (or Explorer) started the launcher from.
func absoluteArgs(in []string) []string {
	out := make([]string, 0, len(in))
	for _, a := range in {
		if a == "" {
			continue
		}
		if strings.HasPrefix(a, "-") {
			out = append(out, a)
			continue
		}
		if abs, err := filepath.Abs(a); err == nil {
			a = abs
		}
		out = append(out, a)
	}
	return out
}

func createMutex(name string) (handle uintptr, existed bool) {
	h, _, err := procCreateMutexW.Call(0, 1, uintptr(unsafe.Pointer(utf16Ptr(name))))
	return h, err == syscall.Errno(errorAlreadyExists)
}

// forward sends the paths to a running copy through WM_COPYDATA. An empty list just brings the
// running copy to the front.
func forward(paths []string) bool {
	hwnd, _, _ := procFindWindowExW.Call(hwndMessage, 0, uintptr(unsafe.Pointer(utf16Ptr(receiverClass))), 0)
	if hwnd == 0 {
		return false
	}
	var pid uint32
	procGetWindowThreadProcessId.Call(hwnd, uintptr(unsafe.Pointer(&pid)))
	if pid != 0 {
		procAllowSetForegroundWindow.Call(uintptr(pid)) // lets the running copy raise its window
	}
	files := make([]string, 0, len(paths))
	for _, p := range paths {
		if !strings.HasPrefix(p, "-") {
			files = append(files, p)
		}
	}
	payload, _ := syscall.UTF16FromString(strings.Join(files, "\x00"))
	payload = payload[:len(payload)-1] // no terminator; an empty payload means "activate"
	cds := copyDataStruct{Data: copyDataMagic, Bytes: uint32(len(payload) * 2)}
	if len(payload) > 0 {
		cds.Ptr = uintptr(unsafe.Pointer(&payload[0]))
	}
	var result uintptr
	ok, _, _ := procSendMessageTimeoutW.Call(hwnd, wmCopyData, 0, uintptr(unsafe.Pointer(&cds)),
		smtoAbortIfHung, 10_000, uintptr(unsafe.Pointer(&result)))
	runtime.KeepAlive(payload)
	return ok != 0 && result == 1
}

func cleanEnvironment(launcher, readyEvent string) []string {
	env := make([]string, 0, len(os.Environ())+2)
	for _, kv := range os.Environ() {
		name, _, _ := strings.Cut(kv, "=")
		blocked := false
		for _, u := range unsafeEnv {
			if strings.EqualFold(name, u) {
				blocked = true
				break
			}
		}
		if !blocked {
			env = append(env, kv)
		}
	}
	return append(env, "ENG_VIEWER_LAUNCHER="+launcher, "ENG_VIEWER_READY_EVENT="+readyEvent)
}

func start(self, core string, args []string, smoke bool) int {
	eventName := fmt.Sprintf(`Local\ENGDrawingViewer.Ready.%d`, os.Getpid())
	ready, _, err := procCreateEventW.Call(0, 1, 0, uintptr(unsafe.Pointer(utf16Ptr(eventName))))
	if ready == 0 {
		fail("Could not start "+appName+".", err)
		return 1
	}
	defer syscall.CloseHandle(syscall.Handle(ready))

	attr := &os.ProcAttr{
		Dir:   filepath.Dir(core),
		Env:   cleanEnvironment(self, eventName),
		Files: []*os.File{os.Stdin, os.Stdout, os.Stderr},
	}
	proc, err := os.StartProcess(core, append([]string{core}, args...), attr)
	if err != nil {
		fail("Could not start "+appName+".", err)
		return 1
	}
	procAllowSetForegroundWindow.Call(uintptr(proc.Pid))
	handle, err := syscall.OpenProcess(synchronize|queryLimitedInfo, false, uint32(proc.Pid))
	if err != nil {
		fail("Could not watch "+appName+" while it starts.", err)
		return 1
	}
	defer syscall.CloseHandle(handle)

	var splash *splashWindow
	if !smoke {
		splash = showSplash(self)
	}
	began := time.Now()
	which := waitWithMessages([]syscall.Handle{handle, syscall.Handle(ready)}, splash)
	if splash != nil {
		splash.close()
	}
	if which == 0 || smoke {
		// The application ended (smoke tests always wait for the end): pass its exit status on.
		syscall.WaitForSingleObject(handle, infinite)
		var code uint32
		syscall.GetExitCodeProcess(handle, &code)
		logf("application exited with status %d after %s", code, time.Since(began).Round(time.Millisecond))
		if code != 0 && !smoke && which == 0 {
			fail(fmt.Sprintf("%s closed unexpectedly while starting (status %d).", appName, code), nil)
		}
		return int(code)
	}
	logf("main window ready after %s", time.Since(began).Round(time.Millisecond))
	return 0
}

// waitWithMessages waits for one of the handles while keeping the splash responsive. It returns the
// index of the handle that was signalled.
func waitWithMessages(handles []syscall.Handle, splash *splashWindow) int {
	deadline := time.Now().Add(10 * time.Minute)
	for {
		r, _, _ := procMsgWaitForMultipleObjects.Call(uintptr(len(handles)), uintptr(unsafe.Pointer(&handles[0])),
			0, 100, qsAllInput)
		switch {
		case r < uintptr(len(handles)):
			return int(r)
		case r == uintptr(len(handles)) || r == waitTimeout:
			pumpMessages()
		default:
			return 0
		}
		if time.Now().After(deadline) {
			// Never keep a splash up forever: stop waiting, the application keeps starting.
			return 1
		}
	}
}

func pumpMessages() {
	var m msg
	for {
		r, _, _ := procPeekMessageW.Call(uintptr(unsafe.Pointer(&m)), 0, 0, 0, pmRemove)
		if r == 0 {
			return
		}
		procTranslateMessage.Call(uintptr(unsafe.Pointer(&m)))
		procDispatchMessageW.Call(uintptr(unsafe.Pointer(&m)))
	}
}

func fail(text string, err error) {
	if err != nil {
		logf("error: %s: %v", text, err)
	} else {
		logf("error: %s", text)
	}
	if os.Getenv("ENG_VIEWER_SMOKE_TEST") == "1" {
		fmt.Fprintln(os.Stderr, text, err)
		return
	}
	messageBox(text+"\n\nDetails are in "+logPath(), appName)
}
