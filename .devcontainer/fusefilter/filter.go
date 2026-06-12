package main

// Visibility policy for the gitignore-driven filter. This file is pure logic and
// is unit-testable on its own (the FUSE wiring lives in main.go). See SANDBOX-PLAN.md §3.3.

import (
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
)

// Sensitive even if git-tracked — matched against the path's basename.
var hardDenyGlobs = []string{
	".env", ".env.*", "*.pem", "*.key", "id_*", "*.p12", "*.pfx",
	".git-credentials", ".netrc", ".npmrc",
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
