package main

// Visibility policy for the gitignore-driven filter. This file is pure logic and
// is unit-testable on its own (the FUSE wiring lives in main.go). See SANDBOX-PLAN.md §3.3.

import (
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
)

// Sensitive even if git-tracked — matched against the path's basename. Applies
// everywhere, including inside force-shown dirs like .claude (see Hidden).
var hardDenyGlobs = []string{
	".env", ".env.*", "*.pem", "*.key", "id_*", "*.p12", "*.pfx",
	".git-credentials", ".netrc", ".npmrc", ".credentials.json",
}

// Force-visible top-level dirs: shown even when gitignored, so the workload can
// read/write them. The hard-deny layer above still hides sensitive files within.
//
//   - .claude holds project-scoped Claude Code state (settings.local.json, local
//     agents/commands, session bits) that the workload legitimately writes.
//   - The build dirs are gitignored but MUST stay visible: compose mounts a named
//     volume over each (/workspace/<dir>), and Docker/runc can only create that
//     mountpoint if the path is not hidden by the filter. Keep this in sync with the
//     build-dir volumes in compose.yaml. The volume masks the host content anyway.
var forceShowDirs = []string{
	".claude",
	"node_modules", ".venv", "target", "dist",
}

// Directories never exposed (matched against the top-level path component).
var hardDenyDirs = []string{".ssh", ".aws", ".gnupg", ".kube", ".docker"}

type Filter struct {
	root    string
	showGit bool
	ign     *ignoreClient
}

func NewFilter(root string, showGit bool) *Filter {
	return &Filter{root: root, showGit: showGit, ign: newIgnoreClient(root)}
}

// Hidden reports whether a path (relative to root; "" == root) must be hidden.
func (f *Filter) Hidden(rel string) bool {
	if rel == "" || rel == "." {
		return false
	}
	rel = filepath.Clean(rel)
	base := filepath.Base(rel)
	top := strings.SplitN(rel, string(filepath.Separator), 2)[0]

	if !f.showGit && top == ".git" {
		return true
	}
	for _, d := range hardDenyDirs {
		if top == d {
			return true
		}
	}
	for _, g := range hardDenyGlobs {
		if ok, _ := filepath.Match(g, base); ok {
			return true
		}
	}
	// Force-shown dirs (e.g. .claude) stay visible/writable even when gitignored.
	// Reached only after the hard-deny layer, so sensitive files within are still hidden.
	for _, d := range forceShowDirs {
		if top == d {
			return false
		}
	}
	// gitignore: hide build artifacts / anything git would ignore.
	return f.ign.Ignored(rel)
}

// ignoreClient answers "would git ignore this path?" with a small cache.
// TODO: replace the per-path `git check-ignore` exec with a long-lived
// `git check-ignore --stdin -z` pipe (or libgit2) for performance on large trees,
// and invalidate the cache when .gitignore files change.
type ignoreClient struct {
	root  string
	mu    sync.Mutex
	cache map[string]bool
}

func newIgnoreClient(root string) *ignoreClient {
	return &ignoreClient{root: root, cache: map[string]bool{}}
}

func (c *ignoreClient) Ignored(rel string) bool {
	c.mu.Lock()
	if v, ok := c.cache[rel]; ok {
		c.mu.Unlock()
		return v
	}
	c.mu.Unlock()

	// exit 0 => ignored, 1 => not ignored, other => error (treat as not ignored).
	cmd := exec.Command("git", "-C", c.root, "check-ignore", "-q", "--", rel)
	ignored := false
	if err := cmd.Run(); err == nil {
		ignored = true
	}

	c.mu.Lock()
	c.cache[rel] = ignored
	c.mu.Unlock()
	return ignored
}
