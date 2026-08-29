package main

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"

	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
)

// mountOwned mounts `source` with the given owner override and returns the mountpoint.
// It skips the test when FUSE is unavailable (no /dev/fuse in CI/containers).
func mountOwned(t *testing.T, source string, own *owner) string {
	t.Helper()
	if _, err := os.Stat("/dev/fuse"); err != nil {
		t.Skipf("no /dev/fuse: %v", err)
	}
	mnt := t.TempDir()
	flt := NewFilter(source, true)

	var st syscall.Stat_t
	if err := syscall.Stat(source, &st); err != nil {
		t.Fatalf("stat source: %v", err)
	}
	root := &fs.LoopbackRoot{Path: source, Dev: uint64(st.Dev)}
	root.NewNode = func(r *fs.LoopbackRoot, _ *fs.Inode, _ string, _ *syscall.Stat_t) fs.InodeEmbedder {
		return &filterNode{LoopbackNode: fs.LoopbackNode{RootData: r}, flt: flt, own: own}
	}

	server, err := fs.Mount(mnt, root.NewNode(root, nil, "", &st), &fs.Options{
		MountOptions: fuse.MountOptions{Name: "gitignore-fuse-test"},
	})
	if err != nil {
		t.Skipf("cannot mount FUSE here: %v", err)
	}
	t.Cleanup(func() {
		if err := server.Unmount(); err != nil {
			t.Errorf("unmount: %v", err)
		}
	})
	return mnt
}

func lstatOwner(t *testing.T, path string) (uint32, uint32) {
	t.Helper()
	var st syscall.Stat_t
	if err := syscall.Lstat(path, &st); err != nil {
		t.Fatalf("lstat %s: %v", path, err)
	}
	return st.Uid, st.Gid
}

// With an override, every visible path reports the configured owner — the root dir, a
// pre-existing file (Lookup/Getattr), a subdir, a symlink, and a file the caller creates
// through the mount (Create). The real files on disk keep their true owner.
func TestOwnerOverrideReportsConfiguredIDs(t *testing.T) {
	const wantUID, wantGID = 4242, 4243

	src := t.TempDir()
	if err := os.WriteFile(filepath.Join(src, "tracked.txt"), []byte("hi"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(src, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("tracked.txt", filepath.Join(src, "link")); err != nil {
		t.Fatal(err)
	}

	mnt := mountOwned(t, src, &owner{uid: wantUID, gid: wantGID})

	for _, name := range []string{"", "tracked.txt", "sub", "link"} {
		uid, gid := lstatOwner(t, filepath.Join(mnt, name))
		if uid != wantUID || gid != wantGID {
			t.Errorf("%q: got owner %d:%d, want %d:%d", name, uid, gid, wantUID, wantGID)
		}
	}

	// Create() through the mount must report the override too.
	created := filepath.Join(mnt, "made.txt")
	if err := os.WriteFile(created, []byte("x"), 0o644); err != nil {
		t.Fatalf("write through mount: %v", err)
	}
	if uid, gid := lstatOwner(t, created); uid != wantUID || gid != wantGID {
		t.Errorf("created file: got owner %d:%d, want %d:%d", uid, gid, wantUID, wantGID)
	}
	// ...while the underlying file belongs to the daemon's real user.
	if uid, _ := lstatOwner(t, filepath.Join(src, "made.txt")); uid != uint32(os.Getuid()) {
		t.Errorf("on-disk file: got uid %d, want the daemon's %d", uid, os.Getuid())
	}
}

// Without an override (the default), the real owner shows through unchanged.
func TestNoOwnerOverridePassesRealIDs(t *testing.T) {
	src := t.TempDir()
	if err := os.WriteFile(filepath.Join(src, "f"), []byte("hi"), 0o644); err != nil {
		t.Fatal(err)
	}
	mnt := mountOwned(t, src, nil)

	uid, gid := lstatOwner(t, filepath.Join(mnt, "f"))
	if uid != uint32(os.Getuid()) || gid != uint32(os.Getgid()) {
		t.Errorf("got owner %d:%d, want the real %d:%d", uid, gid, os.Getuid(), os.Getgid())
	}
}

// apply is nil-safe and overwrites whatever the underlying filesystem reported.
func TestOwnerApply(t *testing.T) {
	var a fuse.Attr
	a.Uid, a.Gid = 1234, 5678
	(*owner)(nil).apply(&a)
	if a.Uid != 1234 || a.Gid != 5678 {
		t.Errorf("nil owner must not change attrs, got %d:%d", a.Uid, a.Gid)
	}
	(&owner{uid: 1000, gid: 1000}).apply(&a)
	if a.Uid != 1000 || a.Gid != 1000 {
		t.Errorf("got %d:%d, want 1000:1000", a.Uid, a.Gid)
	}
}
