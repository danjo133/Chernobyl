// gitignore-fuse: a host-side passthrough filesystem that presents a repo/worktree
// with git-ignored and hard-denied files hidden. The workload binds the filtered
// view, so secrets/artifacts never enter the container. Writes pass through to the
// source. See docs/SANDBOX-PLAN.md §3.3.
//
// UNVERIFIED scaffold — pins hanwen/go-fuse v2. The LoopbackNode embedding and the
// Readdir/Lookup filtering below need a `go build` + mount/read/write test pass.
// Logic (filter.go) is independently testable.
//
// Usage: gitignore-fuse -source DIR -mount DIR [-show-git] [-allow-other] [-uid N] [-gid N]
package main

import (
	"context"
	"flag"
	"log"
	"math"
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
	own *owner
}

// owner rewrites the uid/gid this filesystem REPORTS for every file. The daemon still
// does the real syscalls as its own uid, so files on disk keep belonging to the invoking
// host user; only the view lies. This is what lets a workload container running as uid
// 1000 (`node`) write a repo owned by a host user whose uid is not 1000 — git, node and
// editors all stat() before they write, and an alien st_uid makes them refuse.
//
// fs.Options.UID/GID cannot do this: go-fuse applies them only when the underlying uid is
// 0 (see rawBridge.setAttr), so a repo owned by a normal non-1000 user is left alone. We
// therefore rewrite unconditionally in every attr-bearing node op below.
type owner struct{ uid, gid uint32 }

// apply is nil-safe: no override configured means the real ids pass through untouched.
func (o *owner) apply(a *fuse.Attr) {
	if o == nil {
		return
	}
	a.Uid = o.uid
	a.Gid = o.gid
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
var _ = (fs.NodeGetattrer)((*filterNode)(nil))
var _ = (fs.NodeSetattrer)((*filterNode)(nil))
var _ = (fs.NodeCreater)((*filterNode)(nil))
var _ = (fs.NodeMkdirer)((*filterNode)(nil))
var _ = (fs.NodeMknoder)((*filterNode)(nil))
var _ = (fs.NodeSymlinker)((*filterNode)(nil))
var _ = (fs.NodeLinker)((*filterNode)(nil))

func (n *filterNode) Lookup(ctx context.Context, name string, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	if n.flt.Hidden(n.rel(name)) {
		return nil, syscall.ENOENT
	}
	ch, errno := n.LoopbackNode.Lookup(ctx, name, out)
	if errno == 0 {
		n.own.apply(&out.Attr)
	}
	return ch, errno
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

// The rest of the attr-bearing ops. Readdir needs no override (fuse.DirEntry carries no
// owner) and Readdirplus fills its entries through Lookup above, which does.

func (n *filterNode) Getattr(ctx context.Context, f fs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	errno := n.LoopbackNode.Getattr(ctx, f, out)
	if errno == 0 {
		n.own.apply(&out.Attr)
	}
	return errno
}

func (n *filterNode) Setattr(ctx context.Context, f fs.FileHandle, in *fuse.SetAttrIn, out *fuse.AttrOut) syscall.Errno {
	errno := n.LoopbackNode.Setattr(ctx, f, in, out)
	if errno == 0 {
		n.own.apply(&out.Attr)
	}
	return errno
}

func (n *filterNode) Create(ctx context.Context, name string, flags, mode uint32, out *fuse.EntryOut) (*fs.Inode, fs.FileHandle, uint32, syscall.Errno) {
	ch, fh, fuseFlags, errno := n.LoopbackNode.Create(ctx, name, flags, mode, out)
	if errno == 0 {
		n.own.apply(&out.Attr)
	}
	return ch, fh, fuseFlags, errno
}

func (n *filterNode) Mkdir(ctx context.Context, name string, mode uint32, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	ch, errno := n.LoopbackNode.Mkdir(ctx, name, mode, out)
	if errno == 0 {
		n.own.apply(&out.Attr)
	}
	return ch, errno
}

func (n *filterNode) Mknod(ctx context.Context, name string, mode, rdev uint32, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	ch, errno := n.LoopbackNode.Mknod(ctx, name, mode, rdev, out)
	if errno == 0 {
		n.own.apply(&out.Attr)
	}
	return ch, errno
}

func (n *filterNode) Symlink(ctx context.Context, target, name string, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	ch, errno := n.LoopbackNode.Symlink(ctx, target, name, out)
	if errno == 0 {
		n.own.apply(&out.Attr)
	}
	return ch, errno
}

func (n *filterNode) Link(ctx context.Context, target fs.InodeEmbedder, name string, out *fuse.EntryOut) (*fs.Inode, syscall.Errno) {
	ch, errno := n.LoopbackNode.Link(ctx, target, name, out)
	if errno == 0 {
		n.own.apply(&out.Attr)
	}
	return ch, errno
}

func main() {
	source := flag.String("source", "", "host path to expose (repo or worktree)")
	mount := flag.String("mount", "", "mountpoint for the filtered view")
	showGit := flag.Bool("show-git", true, "expose .git (needed for gitignore eval; hide for extra safety)")
	allowOther := flag.Bool("allow-other", false, "allow other users (needed for ROOTFUL docker bind; needs user_allow_other)")
	uid := flag.Uint("uid", 0, "report every file as owned by this uid (0 = report the real owner)")
	gid := flag.Uint("gid", 0, "report every file as owned by this gid (0 = report the real group)")
	flag.Parse()
	if *source == "" || *mount == "" {
		log.Fatal("both -source and -mount are required")
	}
	if (*uid == 0) != (*gid == 0) {
		log.Fatal("-uid and -gid must be given together (both non-zero, or neither)")
	}
	if *uid > math.MaxUint32 || *gid > math.MaxUint32 {
		log.Fatal("-uid and -gid must fit in 32 bits")
	}
	// Non-zero = "pretend every file belongs to this id". Zero leaves the real owner
	// visible, which is right when the host user's uid already matches the container's.
	var own *owner
	if *uid != 0 {
		own = &owner{uid: uint32(*uid), gid: uint32(*gid)}
	}

	flt := NewFilter(*source, *showGit)

	var st syscall.Stat_t
	if err := syscall.Stat(*source, &st); err != nil {
		log.Fatalf("stat source: %v", err)
	}
	root := &fs.LoopbackRoot{Path: *source, Dev: uint64(st.Dev)}
	root.NewNode = func(r *fs.LoopbackRoot, _ *fs.Inode, _ string, _ *syscall.Stat_t) fs.InodeEmbedder {
		return &filterNode{LoopbackNode: fs.LoopbackNode{RootData: r}, flt: flt, own: own}
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
	log.Printf("gitignore-fuse: %s -> %s (show-git=%v allow-other=%v owner=%d:%d)",
		*source, *mount, *showGit, *allowOther, *uid, *gid)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	go func() { <-sig; server.Unmount() }()
	server.Wait()
}
