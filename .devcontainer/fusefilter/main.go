// gitignore-fuse: a host-side passthrough filesystem that presents a repo/worktree
// with git-ignored and hard-denied files hidden. The workload binds the filtered
// view, so secrets/artifacts never enter the container. Writes pass through to the
// source. See docs/SANDBOX-PLAN.md §3.3.
//
// UNVERIFIED scaffold — pins hanwen/go-fuse v2. The LoopbackNode embedding and the
// Readdir/Lookup filtering below need a `go build` + mount/read/write test pass.
// Logic (filter.go) is independently testable.
//
// Usage: gitignore-fuse -source /host/repo -mount /run/.../view [-show-git] [-allow-other]
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
)

type filterNode struct {
	fs.LoopbackNode
	flt *Filter
}

// rel returns the path of `name` under this node, relative to the mount root.
func (n *filterNode) rel(name string) string {
	p := n.Path(n.Root())
	if p == "" {
		return name
	}
	return filepath.Join(p, name)
}

var _ = (fs.NodeLookuper)((*filterNode)(nil))
var _ = (fs.NodeReaddirer)((*filterNode)(nil))

func (n *filterNode) Lookup(ctx context.Context, name string, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	if n.flt.Hidden(n.rel(name)) {
		return nil, syscall.ENOENT
	}
	return n.LoopbackNode.Lookup(ctx, name, out)
}

func (n *filterNode) Readdir(ctx context.Context) (fs.DirStream, syscall.Errno) {
	ds, errno := n.LoopbackNode.Readdir(ctx)
	if errno != 0 {
		return ds, errno
	}
	base := n.Path(n.Root())
	var kept []fuse.DirEntry
	for ds.HasNext() {
		e, err := ds.Next()
		if err != 0 {
			ds.Close()
			return nil, err
		}
		if n.flt.Hidden(filepath.Join(base, e.Name)) {
			continue
		}
		kept = append(kept, e)
	}
	ds.Close()
	return fs.NewListDirStream(kept), 0
}

func main() {
	source := flag.String("source", "", "host path to expose (repo or worktree)")
	mount := flag.String("mount", "", "mountpoint for the filtered view")
	showGit := flag.Bool("show-git", true, "expose .git (needed for gitignore eval; hide for extra safety)")
	allowOther := flag.Bool("allow-other", false, "allow other users (needed for ROOTFUL docker bind; needs user_allow_other)")
	flag.Parse()
	if *source == "" || *mount == "" {
		log.Fatal("both -source and -mount are required")
	}

	flt := NewFilter(*source, *showGit)

	var st syscall.Stat_t
	if err := syscall.Stat(*source, &st); err != nil {
		log.Fatalf("stat source: %v", err)
	}
	root := &fs.LoopbackRoot{Path: *source, Dev: uint64(st.Dev)}
	root.NewNode = func(r *fs.LoopbackRoot, _ *fs.Inode, _ string, _ *syscall.Stat_t) fs.InodeEmbedder {
		return &filterNode{LoopbackNode: fs.LoopbackNode{RootData: r}, flt: flt}
	}
	rootNode := root.NewNode(root, nil, "", &st)

	server, err := fs.Mount(*mount, rootNode, &fs.Options{
		MountOptions: fuse.MountOptions{
			AllowOther: *allowOther,
			FsName:     *source,
			Name:       "gitignore-fuse",
		},
	})
	if err != nil {
		log.Fatalf("mount: %v", err)
	}
	log.Printf("gitignore-fuse: %s -> %s (show-git=%v allow-other=%v)", *source, *mount, *showGit, *allowOther)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	go func() { <-sig; server.Unmount() }()
	server.Wait()
}
