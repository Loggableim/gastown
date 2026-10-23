//go:build !windows

package nudge

import (
	"fmt"
	"os"
	"strings"
	"syscall"
)

func pollerProcessAlive(pid int) bool {
	if pid <= 0 {
		return false
	}

	proc, err := os.FindProcess(pid)
	if err != nil {
		return false
	}

	if proc.Signal(syscall.Signal(0)) != nil {
		return false
	}

	// On Linux, a zombie (defunct) process responds to signal 0 with success
	// because it remains in the process table until reaped by its parent.
	// Check /proc/<pid>/status to ensure the process is genuinely active.
	if statusData, err := os.ReadFile(fmt.Sprintf("/proc/%d/status", pid)); err == nil {
		for _, line := range strings.Split(string(statusData), "\n") {
			if strings.HasPrefix(line, "State:") {
				fields := strings.Fields(line)
				if len(fields) >= 2 && (fields[1] == "Z" || fields[1] == "X") {
					return false // Zombie or dead process
				}
				break
			}
		}
	}

	return true
}
