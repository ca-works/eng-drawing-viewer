// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright © 2026 ca-works

//go:build windows

package main

import (
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// version is set at build time: go build -ldflags "-X main.version=2.2.0".
var version = "dev"

const maxLogBytes = 256 * 1024

// logPath is the launcher log: in the per-user application data folder, the same folder the
// application writes its error log to. The application folder itself may be read-only (a ZIP opened
// in place, a locked-down drive), so it is never used. Without LOCALAPPDATA the temporary folder is.
func logPath() string {
	base := os.Getenv("LOCALAPPDATA")
	if base == "" {
		base = os.TempDir()
	}
	return filepath.Join(base, appName, "Logs", "launcher.log")
}

// logf appends one line. Logging must never stop the application from starting, so every error
// here is ignored. File names are not logged.
func logf(format string, args ...any) {
	path := logPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return
	}
	if st, err := os.Stat(path); err == nil && st.Size() > maxLogBytes {
		_ = os.Rename(path, path+".1")
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	defer f.Close()
	fmt.Fprintf(f, "%s [%d] %s\n", time.Now().Format("2006-01-02 15:04:05"), os.Getpid(), fmt.Sprintf(format, args...))
}
