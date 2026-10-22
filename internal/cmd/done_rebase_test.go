package cmd

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	gitpkg "github.com/steveyegge/gastown/internal/git"
)

// fakeRebaseGit lets us drive autoRebaseOnTarget without a real git repo for
// the gating-decision tests.
type fakeRebaseGit struct {
	rebaseErr   error
	rebaseCalls int
	abortCalls  int
}

func (f *fakeRebaseGit) Rebase(onto string) error {
	f.rebaseCalls++
	return f.rebaseErr
}

func (f *fakeRebaseGit) AbortRebase() error {
	f.abortCalls++
	return nil
}

// TestAutoRebaseOnTarget_GatingDecisions verifies the skip/rebase decision
// matrix (gh#3400). The behavior under test is the *decision*, not the actual
// git mechanics — those are exercised separately below against a real repo.
func TestAutoRebaseOnTarget_GatingDecisions(t *testing.T) {
	tests := []struct {
		name          string
		behind        int
		preVerified   bool
		alreadyPushed bool
		wantRebased   bool
		wantSkip      string
		wantCalls     int
	}{
		{
			name:        "not behind: no-op",
			behind:      0,
			wantRebased: false,
			wantSkip:    "",
			wantCalls:   0,
		},
		{
			name:        "behind by 1: rebase runs",
			behind:      1,
			wantRebased: true,
			wantSkip:    "",
			wantCalls:   1,
		},
		{
			name:        "behind by 5: rebase runs",
			behind:      5,
			wantRebased: true,
			wantSkip:    "",
			wantCalls:   1,
		},
		{
			name:        "pre-verified: skip even when behind",
			behind:      3,
			preVerified: true,
			wantRebased: false,
			wantSkip:    "--pre-verified is set",
			wantCalls:   0,
		},
		{
			name:          "already pushed: skip to avoid divergence",
			behind:        3,
			alreadyPushed: true,
			wantRebased:   false,
			wantSkip:      "prior push checkpoint exists",
			wantCalls:     0,
		},
		{
			name:          "pre-verified takes precedence over already-pushed",
			behind:        3,
			preVerified:   true,
			alreadyPushed: true,
			wantRebased:   false,
			wantSkip:      "--pre-verified is set",
			wantCalls:     0,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			fake := &fakeRebaseGit{}
			rebased, skipReason, err := autoRebaseOnTarget(fake, "origin/main", tt.behind, tt.preVerified, tt.alreadyPushed)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if rebased != tt.wantRebased {
				t.Errorf("rebased = %v, want %v", rebased, tt.wantRebased)
			}
			if skipReason != tt.wantSkip {
				t.Errorf("skipReason = %q, want %q", skipReason, tt.wantSkip)
			}
			if fake.rebaseCalls != tt.wantCalls {
				t.Errorf("rebase calls = %d, want %d", fake.rebaseCalls, tt.wantCalls)
			}
			if fake.abortCalls != 0 {
				t.Errorf("abort calls = %d on success path, want 0", fake.abortCalls)
			}
		})
	}
}

// TestAutoRebaseOnTarget_ConflictAborts verifies that a rebase failure causes
// AbortRebase to fire and the returned error includes remediation guidance.
func TestAutoRebaseOnTarget_ConflictAborts(t *testing.T) {
	fake := &fakeRebaseGit{rebaseErr: errors.New("CONFLICT (content): merge conflict in foo.txt")}

	rebased, skipReason, err := autoRebaseOnTarget(fake, "origin/main", 1, false, false)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if rebased {
		t.Error("rebased should be false on conflict")
	}
	if skipReason != "" {
		t.Errorf("skipReason should be empty on conflict, got %q", skipReason)
	}
	if fake.rebaseCalls != 1 {
		t.Errorf("expected 1 rebase call, got %d", fake.rebaseCalls)
	}
	if fake.abortCalls != 1 {
		t.Errorf("expected AbortRebase to fire on conflict, got %d calls", fake.abortCalls)
	}
	// Remediation guidance is part of the contract — agents read this message.
	msg := err.Error()
	if !strings.Contains(msg, "auto-rebase onto origin/main failed") {
		t.Errorf("error missing context: %q", msg)
	}
	if !strings.Contains(msg, "git rebase origin/main") {
		t.Errorf("error missing remediation hint: %q", msg)
	}
	if !strings.Contains(msg, "rerun gt done") {
		t.Errorf("error missing rerun hint: %q", msg)
	}
}

// TestAutoRebaseOnTarget_RealRepoSuccess exercises the rebase against a real
// git working tree to confirm the wiring (Rebase call) actually replays the
// branch onto a moved base. (gh#3400, scenario (a) from the bead notes.)
func TestAutoRebaseOnTarget_RealRepoSuccess(t *testing.T) {
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	testRunGit(t, tmp, "init", "--initial-branch", "main", repo)
	testRunGit(t, repo, "config", "user.email", "test@test.com")
	testRunGit(t, repo, "config", "user.name", "Test")

	// Initial commit on main.
	writeRepoFile(t, repo, "README.md", "# initial\n")
	testRunGit(t, repo, "add", ".")
	testRunGit(t, repo, "commit", "-m", "initial")

	// Branch off and add a polecat commit on a non-conflicting file.
	testRunGit(t, repo, "checkout", "-b", "feature")
	writeRepoFile(t, repo, "feature.txt", "feature work\n")
	testRunGit(t, repo, "add", ".")
	testRunGit(t, repo, "commit", "-m", "feature")

	// Move main forward independently — also non-conflicting with feature.
	testRunGit(t, repo, "checkout", "main")
	writeRepoFile(t, repo, "main-new.txt", "new on main\n")
	testRunGit(t, repo, "add", ".")
	testRunGit(t, repo, "commit", "-m", "advance main")

	testRunGit(t, repo, "checkout", "feature")

	g := gitpkg.NewGit(repo)
	rebased, skipReason, err := autoRebaseOnTarget(g, "main", 1, false, false)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !rebased {
		t.Fatalf("expected rebased=true, got false (skip=%q)", skipReason)
	}

	// After rebase, both files must be present and the feature commit must sit
	// on top of the advance-main commit.
	if _, statErr := os.Stat(filepath.Join(repo, "main-new.txt")); statErr != nil {
		t.Errorf("main-new.txt missing after rebase: %v", statErr)
	}
	if _, statErr := os.Stat(filepath.Join(repo, "feature.txt")); statErr != nil {
		t.Errorf("feature.txt missing after rebase: %v", statErr)
	}
}

// TestAutoRebaseOnTarget_RealRepoConflictAborts exercises the conflict path
// against a real git working tree: feature and main both touch the same file,
// rebase fails with a CONFLICT, and AbortRebase must restore the working tree
// so the polecat can address the conflict manually. (gh#3400, scenario (b).)
func TestAutoRebaseOnTarget_RealRepoConflictAborts(t *testing.T) {
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	testRunGit(t, tmp, "init", "--initial-branch", "main", repo)
	testRunGit(t, repo, "config", "user.email", "test@test.com")
	testRunGit(t, repo, "config", "user.name", "Test")

	writeRepoFile(t, repo, "shared.txt", "v0\n")
	testRunGit(t, repo, "add", ".")
	testRunGit(t, repo, "commit", "-m", "initial")

	// Feature changes shared.txt to v1.
	testRunGit(t, repo, "checkout", "-b", "feature")
	writeRepoFile(t, repo, "shared.txt", "v1-feature\n")
	testRunGit(t, repo, "add", ".")
	testRunGit(t, repo, "commit", "-m", "feature edit")

	// Main changes shared.txt to a different value — guaranteed conflict.
	testRunGit(t, repo, "checkout", "main")
	writeRepoFile(t, repo, "shared.txt", "v1-main\n")
	testRunGit(t, repo, "add", ".")
	testRunGit(t, repo, "commit", "-m", "main edit")

	testRunGit(t, repo, "checkout", "feature")

	g := gitpkg.NewGit(repo)
	rebased, skipReason, err := autoRebaseOnTarget(g, "main", 1, false, false)
	if err == nil {
		t.Fatal("expected conflict error, got nil")
	}
	if rebased {
		t.Error("rebased should be false on conflict")
	}
	if skipReason != "" {
		t.Errorf("skipReason should be empty on conflict, got %q", skipReason)
	}

	// AbortRebase is required to leave the working tree in a clean state. After
	// abort, the rebase-merge dir must be gone — otherwise the polecat is stuck
	// in a half-rebased state.
	if _, statErr := os.Stat(filepath.Join(repo, ".git", "rebase-merge")); !os.IsNotExist(statErr) {
		t.Errorf(".git/rebase-merge should not exist after abort (stat err: %v)", statErr)
	}
	if _, statErr := os.Stat(filepath.Join(repo, ".git", "rebase-apply")); !os.IsNotExist(statErr) {
		t.Errorf(".git/rebase-apply should not exist after abort (stat err: %v)", statErr)
	}
}

func writeRepoFile(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0644); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
}

func TestPlanAutoRebase(t *testing.T) {
	const mainRef = "origin/main"
	const publishedRef = "origin/work/demo"

	tests := []struct {
		name             string
		behindTarget     int
		behindPublished  int
		aheadPublished   int
		published        bool
		publishedIsBase  bool
		wantBase         string
		wantBehind       int
		wantSkipContains string
	}{
		{
			name:         "unpublished behind main rebases onto main",
			behindTarget: 4,
			wantBase:     mainRef,
			wantBehind:   4,
		},
		{
			name: "unpublished and current is a no-op",
		},
		{
			name:             "published, fast-forward with remote, not behind remote: skip main",
			behindTarget:     4,
			published:        true,
			wantSkipContains: "already published",
		},
		{
			name:            "published and strictly behind remote: rebase onto remote",
			behindTarget:    4,
			behindPublished: 1,
			published:       true,
			wantBase:        publishedRef,
			wantBehind:      1,
		},
		{
			name:             "published and diverged: do not rebase onto main",
			behindTarget:     4,
			behindPublished:  1,
			aheadPublished:   2,
			published:        true,
			wantSkipContains: "diverged from",
		},
		{
			name:             "published, caught up with main, diverged from remote: still skip",
			behindPublished:  1,
			aheadPublished:   2,
			published:        true,
			wantSkipContains: "diverged from",
		},
		{
			name:            "published flag ignored when ref equals merge target",
			behindTarget:    2,
			published:       true,
			publishedIsBase: true,
			wantBase:        mainRef,
			wantBehind:      2,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ref := publishedRef
			if tt.publishedIsBase {
				ref = mainRef
			}
			plan := planAutoRebase(mainRef, ref, tt.behindTarget, tt.behindPublished, tt.aheadPublished, tt.published)
			if plan.base != tt.wantBase {
				t.Errorf("base = %q, want %q", plan.base, tt.wantBase)
			}
			if plan.behind != tt.wantBehind {
				t.Errorf("behind = %d, want %d", plan.behind, tt.wantBehind)
			}
			if tt.wantSkipContains == "" && plan.skipReason != "" {
				t.Errorf("skipReason = %q, want empty", plan.skipReason)
			}
			if tt.wantSkipContains != "" && !strings.Contains(plan.skipReason, tt.wantSkipContains) {
				t.Errorf("skipReason = %q, want it to contain %q", plan.skipReason, tt.wantSkipContains)
			}
		})
	}
}

// seedPublishedRepo builds a bare origin and a clone whose work/demo branch
// is pushed, then advances origin/main by one commit. The clone is left on
// work/demo with remote-tracking refs fetched. Returns the clone path.
func seedPublishedRepo(t *testing.T) string {
	t.Helper()
	tmp := t.TempDir()
	origin := filepath.Join(tmp, "origin.git")
	testRunGit(t, tmp, "init", "--bare", "--initial-branch", "main", origin)

	clone := filepath.Join(tmp, "clone")
	testRunGit(t, tmp, "init", "--initial-branch", "main", clone)
	testRunGit(t, clone, "config", "user.email", "test@test.com")
	testRunGit(t, clone, "config", "user.name", "Test")
	writeRepoFile(t, clone, "README.md", "v1\n")
	testRunGit(t, clone, "add", ".")
	testRunGit(t, clone, "commit", "-m", "initial")
	testRunGit(t, clone, "remote", "add", "origin", origin)
	testRunGit(t, clone, "push", "-u", "origin", "main")

	testRunGit(t, clone, "checkout", "-b", "work/demo")
	writeRepoFile(t, clone, "feature.txt", "feature\n")
	testRunGit(t, clone, "add", ".")
	testRunGit(t, clone, "commit", "-m", "feature")
	testRunGit(t, clone, "push", "-u", "origin", "work/demo")

	testRunGit(t, clone, "checkout", "main")
	writeRepoFile(t, clone, "main-new.txt", "new on main\n")
	testRunGit(t, clone, "add", ".")
	testRunGit(t, clone, "commit", "-m", "advance main")
	testRunGit(t, clone, "push", "origin", "main")

	testRunGit(t, clone, "checkout", "work/demo")
	testRunGit(t, clone, "fetch", "origin")
	return clone
}

func headSHA(t *testing.T, repo string) string {
	t.Helper()
	cmd := exec.Command("git", "rev-parse", "HEAD")
	cmd.Dir = repo
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("rev-parse HEAD: %v", err)
	}
	return strings.TrimSpace(string(out))
}

func pushRefspec(t *testing.T, repo, refspec string) (string, error) {
	t.Helper()
	cmd := exec.Command("git", "push", "origin", refspec)
	cmd.Dir = repo
	out, err := cmd.CombinedOutput()
	return string(out), err
}

// TestPublishedBranchSkipsRebaseOntoMain is the sk-xsn loop: the branch is
// already on origin, main has moved, and rebasing onto origin/main would make
// the following non-force push fail. The published tip must stay put and the
// push must succeed.
func TestPublishedBranchSkipsRebaseOntoMain(t *testing.T) {
	clone := seedPublishedRepo(t)
	g := gitpkg.NewGit(clone)

	before := headSHA(t, clone)
	publishedRef, published, behind, ahead, err := detectPublishedBranch(g, "work/demo", "origin/main")
	if err != nil {
		t.Fatalf("detectPublishedBranch: %v", err)
	}
	if !published || publishedRef != "origin/work/demo" {
		t.Fatalf("published = %v ref = %q, want origin/work/demo", published, publishedRef)
	}
	if behind != 0 || ahead != 0 {
		t.Fatalf("relative to published tip: behind=%d ahead=%d, want 0/0", behind, ahead)
	}

	contam, err := g.CheckBranchContamination("origin/main")
	if err != nil {
		t.Fatalf("contamination: %v", err)
	}
	if contam.Behind == 0 {
		t.Fatal("expected branch to be behind origin/main")
	}
	plan := planAutoRebase("origin/main", publishedRef, contam.Behind, behind, ahead, published)
	if plan.behind != 0 {
		t.Fatalf("plan.behind = %d, want 0 (do not rebase): %+v", plan.behind, plan)
	}
	if !strings.Contains(plan.skipReason, "already published") {
		t.Fatalf("skipReason = %q, want already-published explanation", plan.skipReason)
	}
	if headSHA(t, clone) != before {
		t.Fatal("planning the rebase rewrote HEAD")
	}

	if out, pushErr := pushRefspec(t, clone, "work/demo:work/demo"); pushErr != nil {
		t.Fatalf("push of untouched published branch failed: %v\n%s", pushErr, out)
	}
}

// TestUnpublishedBranchStillRebasesOntoMain keeps the gh#3400 first-push
// behavior: a branch origin does not have yet still rebases onto main, and
// that push then creates the remote branch.
func TestUnpublishedBranchStillRebasesOntoMain(t *testing.T) {
	clone := seedPublishedRepo(t)
	// Drop the remote feature branch so this looks like a first push.
	testRunGit(t, clone, "push", "origin", "--delete", "work/demo")
	testRunGit(t, clone, "fetch", "origin", "--prune")

	g := gitpkg.NewGit(clone)
	_, published, _, _, err := detectPublishedBranch(g, "work/demo", "origin/main")
	if err != nil {
		t.Fatalf("detectPublishedBranch: %v", err)
	}
	if published {
		t.Fatal("branch should not be published after remote delete")
	}
	contam, err := g.CheckBranchContamination("origin/main")
	if err != nil {
		t.Fatalf("contamination: %v", err)
	}
	plan := planAutoRebase("origin/main", "origin/work/demo", contam.Behind, 0, 0, false)
	if plan.base != "origin/main" || plan.behind != contam.Behind {
		t.Fatalf("plan = %+v, want rebase onto origin/main", plan)
	}
	rebased, skipReason, rebaseErr := autoRebaseOnTarget(g, plan.base, plan.behind, false, false)
	if rebaseErr != nil {
		t.Fatalf("rebase: %v", rebaseErr)
	}
	if !rebased || skipReason != "" {
		t.Fatalf("rebased=%v skip=%q, want rebase", rebased, skipReason)
	}
	if _, statErr := os.Stat(filepath.Join(clone, "main-new.txt")); statErr != nil {
		t.Errorf("main-new.txt missing after rebase: %v", statErr)
	}
	if _, statErr := os.Stat(filepath.Join(clone, "feature.txt")); statErr != nil {
		t.Errorf("feature.txt missing after rebase: %v", statErr)
	}
	if out, pushErr := pushRefspec(t, clone, "work/demo:work/demo"); pushErr != nil {
		t.Fatalf("first push after rebase onto main failed: %v\n%s", pushErr, out)
	}
}

// TestDivergedPublishedBranchDoesNotRebaseOntoMain covers the state left by
// the old bug: local was rebased onto main, origin still has the previous
// tip, patch-id matches. gt done must not rebase onto main again.
func TestDivergedPublishedBranchDoesNotRebaseOntoMain(t *testing.T) {
	clone := seedPublishedRepo(t)
	testRunGit(t, clone, "rebase", "origin/main")
	rewritten := headSHA(t, clone)

	g := gitpkg.NewGit(clone)
	publishedRef, published, behind, ahead, err := detectPublishedBranch(g, "work/demo", "origin/main")
	if err != nil {
		t.Fatalf("detectPublishedBranch: %v", err)
	}
	if !published {
		t.Fatal("expected published branch")
	}
	if behind == 0 || ahead == 0 {
		t.Fatalf("expected divergence, behind=%d ahead=%d", behind, ahead)
	}
	contam, err := g.CheckBranchContamination("origin/main")
	if err != nil {
		t.Fatalf("contamination: %v", err)
	}
	plan := planAutoRebase("origin/main", publishedRef, contam.Behind, behind, ahead, published)
	if plan.behind != 0 || !strings.Contains(plan.skipReason, "diverged from") {
		t.Fatalf("plan = %+v, want skip on divergence", plan)
	}
	if headSHA(t, clone) != rewritten {
		t.Fatal("HEAD changed despite skip")
	}
	if _, pushErr := pushRefspec(t, clone, "work/demo:work/demo"); pushErr == nil {
		t.Fatal("diverged push should still be rejected; the fix is to stop rebasing, not to force-push")
	}
}

// TestStrictlyBehindPublishedBranchFastForwards rebases onto origin/<branch>
// when the remote tip moved and local has no unique commits. The push after
// that rebase is a fast-forward.
func TestStrictlyBehindPublishedBranchFastForwards(t *testing.T) {
	clone := seedPublishedRepo(t)
	writeRepoFile(t, clone, "more.txt", "more\n")
	testRunGit(t, clone, "add", ".")
	testRunGit(t, clone, "commit", "-m", "remote moves")
	testRunGit(t, clone, "push", "origin", "work/demo")
	testRunGit(t, clone, "reset", "--hard", "HEAD~1")
	testRunGit(t, clone, "fetch", "origin")

	g := gitpkg.NewGit(clone)
	publishedRef, published, behind, ahead, err := detectPublishedBranch(g, "work/demo", "origin/main")
	if err != nil {
		t.Fatalf("detectPublishedBranch: %v", err)
	}
	if !published || behind == 0 || ahead != 0 {
		t.Fatalf("published=%v behind=%d ahead=%d, want strictly behind", published, behind, ahead)
	}
	contam, contamErr := g.CheckBranchContamination("origin/main")
	if contamErr != nil {
		t.Fatalf("contamination: %v", contamErr)
	}
	plan := planAutoRebase("origin/main", publishedRef, contam.Behind, behind, ahead, published)
	if plan.base != publishedRef || plan.behind != behind {
		t.Fatalf("plan = %+v, want rebase onto %s", plan, publishedRef)
	}
	rebased, skipReason, rebaseErr := autoRebaseOnTarget(g, plan.base, plan.behind, false, false)
	if rebaseErr != nil {
		t.Fatalf("rebase: %v", rebaseErr)
	}
	if !rebased {
		t.Fatalf("expected rebase, skip=%q", skipReason)
	}
	remoteTip, revErr := g.Rev(publishedRef)
	if revErr != nil {
		t.Fatalf("rev: %v", revErr)
	}
	if headSHA(t, clone) != remoteTip {
		t.Fatalf("HEAD = %s, published tip = %s", headSHA(t, clone), remoteTip)
	}
	if out, pushErr := pushRefspec(t, clone, "work/demo:work/demo"); pushErr != nil {
		t.Fatalf("fast-forward push failed: %v\n%s", pushErr, out)
	}
}
