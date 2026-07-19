package main

import (
	"os"
	"path/filepath"
	"testing"
)

// Hard-deny must hide sensitive files/dirs at ANY depth, and independently of git —
// these cases never reach `git check-ignore`, so no repo is needed.
func TestHiddenHardDenyAnyDepth(t *testing.T) {
	f := NewFilter(t.TempDir(), true)
	hidden := []string{
		".env", "a/b/.env", "sub/.env.local",
		"x/y/id_rsa", "deep/nested/server.key", "certs/ca.pem", "p/store.p12",
		".ssh", "infra/.aws", "infra/.aws/credentials",
		"team/.gnupg/secring.gpg", "k/.kube/config", "d/.docker/config.json",
		".git-credentials", "svc/.netrc", "app/.npmrc",
	}
	for _, c := range hidden {
		if !f.Hidden(c) {
			t.Errorf("expected HIDDEN, got visible: %q", c)
		}
	}
}

// .sandboxshow force-shows git-ignored paths (caches) so the workload can read/write them.
func TestShowListForcesVisible(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, ".sandboxshow"),
		[]byte("# caches\n.cache/\nbuild/output\n*.keep\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	f := NewFilter(root, true)
	for _, c := range []string{
		".cache", ".cache/deep/file", // name entry, any depth
		"build/output", "build/output/x", // path entry + subtree
		"keep.keep", "a/b/thing.keep", // glob per component
		"node_modules/pkg", // built-in still honored
	} {
		if f.Hidden(c) {
			t.Errorf("expected VISIBLE (force-shown), got hidden: %q", c)
		}
	}
}

// A secret listed in .sandboxshow must STILL be hidden — hard-deny always wins.
func TestShowListNeverOverridesHardDeny(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, ".sandboxshow"),
		[]byte(".env\n.ssh\n*.key\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	f := NewFilter(root, true)
	for _, c := range []string{".env", "sub/.env", ".ssh", "a/b/.ssh", "x/private.key"} {
		if !f.Hidden(c) {
			t.Errorf("hard-deny must override .sandboxshow for %q", c)
		}
	}
}
