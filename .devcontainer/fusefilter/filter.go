package main

// Visibility policy for the gitignore-driven filter. This file is pure logic and
// is unit-testable on its own (the FUSE wiring lives in main.go). See SANDBOX-PLAN.md §3.3.

import (
	"os"
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

// Built-in force-show set: shown even when gitignored, so the workload can read/write
// them. The hard-deny layer above still hides sensitive files within. Per-repo entries
// from a committed `.sandboxshow` file are merged on top (see showList).
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
	show    *showList
}

func NewFilter(root string, showGit bool) *Filter {
	return &Filter{root: root, showGit: showGit, ign: newIgnoreClient(root), show: newShowList(root)}
}

// Hidden reports whether a path (relative to root; "" == root) must be hidden.
func (f *Filter) Hidden(rel string) bool {
	if rel == "" || rel == "." {
		return false
	}
	rel = filepath.Clean(rel)
	base := filepath.Base(rel)
	parts := strings.Split(rel, string(filepath.Separator))
	top := parts[0]

	if !f.showGit && top == ".git" {
		return true
	}
	// Hard-deny sensitive directories at ANY depth, not just the top level: a
	// git-tracked nested secret dir (e.g. infra/.aws, service/.ssh) must stay hidden
	// too. Match every path component against the denylist.
	for _, part := range parts {
		for _, d := range hardDenyDirs {
			if part == d {
				return true
			}
		}
	}
	for _, g := range hardDenyGlobs {
		if ok, _ := filepath.Match(g, base); ok {
			return true
		}
	}
	// Force-shown paths (built-in set + repo's .sandboxshow) stay visible/writable even
	// when gitignored — this is how a git-ignored cache is made read/write in the sandbox
	// without ever being committed. Reached only AFTER the hard-deny layer, so a secret
	// can never be revealed by listing it here.
	if f.show.shows(rel) {
		return false
	}
	// gitignore: hide build artifacts / anything git would ignore.
	return f.ign.Ignored(rel)
}

// showList decides whether a git-ignored path is nonetheless force-visible in the
// sandbox. It merges the built-in forceShowDirs (which MUST stay visible) with per-repo
// entries read from a committed `.sandboxshow` file at the source root. This decouples
// "hidden from the sandbox" from "ignored by git": a cache dir can be in .gitignore (never
// committed) yet listed in .sandboxshow (read/write inside the sandbox).
//
// Syntax (small, gitignore-like): blank lines and `# comments` are skipped; a trailing
// slash is optional. An entry with NO slash matches that name as any path component (so
// `.cache` or `node_modules` is shown wherever it appears, and shell globs like `*.tmp`
// are honored per component); an entry WITH a slash matches that exact path and everything
// beneath it. The hard-deny layer always wins, so listing a secret here cannot expose it.
type showList struct {
	names []string // component-name / glob entries, matched against each path component
	paths []string // slash-bearing path entries, prefix-matched against rel
}

func newShowList(root string) *showList {
	s := &showList{names: append([]string(nil), forceShowDirs...)}
	data, err := os.ReadFile(filepath.Join(root, ".sandboxshow"))
	if err != nil {
		return s // no file (or unreadable) -> built-ins only
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = filepath.Clean(strings.TrimSuffix(line, "/"))
		if line == "." || line == "" {
			continue
		}
		if strings.Contains(line, string(filepath.Separator)) {
			s.paths = append(s.paths, line)
		} else {
			s.names = append(s.names, line)
		}
	}
	return s
}

func (s *showList) shows(rel string) bool {
	comps := strings.Split(rel, string(filepath.Separator))
	for _, name := range s.names {
		for _, c := range comps {
			if c == name {
				return true
			}
			if ok, _ := filepath.Match(name, c); ok {
				return true
			}
		}
	}
	for _, p := range s.paths {
		if rel == p || strings.HasPrefix(rel, p+string(filepath.Separator)) {
			return true
		}
	}
	return false
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
