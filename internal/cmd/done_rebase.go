package cmd

import (
	"fmt"
)

// rebaseGit is the subset of *git.Git that autoRebaseOnTarget needs. Defined as
// an interface so tests can drive the decision logic without standing up a full
// git repo for every gating case.
type rebaseGit interface {
	Rebase(onto string) error
	AbortRebase() error
}

// autoRebaseOnTarget rebases the current branch onto base when the branch is
// behind the target. It is a no-op when there is nothing to rebase, when the
// polecat ran the formula's pre-verify step (rebasing again would invalidate
// the gate results that --pre-verified attests to), or when a prior push
// checkpoint exists (rebasing after pushing would require a force-push).
//
// Returns:
//   - rebased: true if a rebase actually ran successfully.
//   - skipReason: non-empty when behind > 0 but the rebase was intentionally
//     skipped. Empty when behind == 0 (no rebase needed) or when rebased == true.
//   - err: rebase failure, after AbortRebase has been attempted to clean up.
//
// gh#3400.
func autoRebaseOnTarget(g rebaseGit, base string, behind int, preVerified, alreadyPushed bool) (rebased bool, skipReason string, err error) {
	if behind <= 0 {
		return false, "", nil
	}
	switch {
	case preVerified:
		return false, "--pre-verified is set", nil
	case alreadyPushed:
		return false, "prior push checkpoint exists", nil
	}

	fmt.Printf("→ Auto-rebasing onto %s (%d commits behind)\n", base, behind)
	if rebaseErr := g.Rebase(base); rebaseErr != nil {
		_ = g.AbortRebase()
		return false, "", fmt.Errorf("auto-rebase onto %s failed: %w\n"+
			"Resolve conflicts manually (git fetch origin && git rebase %s), commit the resolution, then rerun gt done.",
			base, rebaseErr, base)
	}
	return true, "", nil
}

// publishedBranchGit is the subset of *git.Git that detectPublishedBranch needs.
type publishedBranchGit interface {
	RemoteTrackingBranchExists(remote, branch string) (bool, error)
	CountCommitsBehind(ref string) (int, error)
	CommitsAhead(base, branch string) (int, error)
}

// autoRebasePlan is the rebase gt done should attempt before pushing.
// behind == 0 means do not rebase. skipReason explains a deliberate skip
// when the branch is behind the merge target but must not be rewritten.
type autoRebasePlan struct {
	base       string
	behind     int
	skipReason string
}

// detectPublishedBranch reports whether origin/<branch> already exists as a
// remote-tracking ref, and how HEAD sits relative to that tip.
//
// A detached HEAD ("HEAD") and the merge-target branch itself are not
// published feature branches. When the ref exists but the ahead/behind counts
// cannot be read, published is still true with zero distance: callers must
// skip a rewrite onto the merge target rather than guess.
//
// sk-xsn.
func detectPublishedBranch(g publishedBranchGit, branch, contaminationBase string) (publishedRef string, published bool, behind, ahead int, err error) {
	if branch == "" || branch == "HEAD" {
		return "", false, 0, 0, nil
	}
	publishedRef = "origin/" + branch
	if publishedRef == contaminationBase {
		return publishedRef, false, 0, 0, nil
	}
	exists, existsErr := g.RemoteTrackingBranchExists("origin", branch)
	if existsErr != nil || !exists {
		return publishedRef, false, 0, 0, existsErr
	}
	behind, err = g.CountCommitsBehind(publishedRef)
	if err != nil {
		return publishedRef, true, 0, 0, err
	}
	ahead, err = g.CommitsAhead(publishedRef, "HEAD")
	if err != nil {
		return publishedRef, true, 0, 0, err
	}
	return publishedRef, true, behind, ahead, nil
}

// planAutoRebase decides where, if anywhere, to rebase before push.
//
// Unpublished branches rebase onto the merge target (typically origin/main)
// so the first push is current (gh#3400). Published branches — origin/<branch>
// already exists — must not rebase onto the merge target. That rewrites
// commits the remote already has, and the later non-force push of
// branch:branch is rejected (sk-xsn). The next gt done then rebases again.
//
// A published branch rebases onto its own remote tip only when HEAD is
// strictly behind that tip, which is a fast-forward. A diverged published
// branch is left untouched: replaying local commits onto the remote tip can
// pull merge-target commits onto the PR, and rebasing onto the merge target
// repeats the non-fast-forward failure.
func planAutoRebase(contaminationBase, publishedRef string, behindTarget, behindPublished, aheadPublished int, published bool) autoRebasePlan {
	if published && publishedRef != "" && publishedRef != contaminationBase {
		switch {
		case behindPublished > 0 && aheadPublished == 0:
			return autoRebasePlan{base: publishedRef, behind: behindPublished}
		case behindPublished > 0 && aheadPublished > 0:
			return autoRebasePlan{
				skipReason: fmt.Sprintf("diverged from %s (%d ahead, %d behind); not rebasing onto %s (push would be non-fast-forward). If these commits are rewritten copies of the remote branch, recover with: git reset --hard %s",
					publishedRef, aheadPublished, behindPublished, contaminationBase, publishedRef),
			}
		case behindTarget > 0:
			return autoRebasePlan{
				skipReason: fmt.Sprintf("%s is already published; not rebasing onto %s", publishedRef, contaminationBase),
			}
		default:
			return autoRebasePlan{}
		}
	}
	if behindTarget > 0 {
		return autoRebasePlan{base: contaminationBase, behind: behindTarget}
	}
	return autoRebasePlan{}
}
