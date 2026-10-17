package cmd

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/steveyegge/gastown/internal/beads"
	"github.com/steveyegge/gastown/internal/constants"
)

func TestGetFormulaNames(t *testing.T) {
	// Create temp directory structure
	tmpDir := t.TempDir()
	formulasDir := filepath.Join(tmpDir, "formulas")
	if err := os.MkdirAll(formulasDir, 0755); err != nil {
		t.Fatalf("creating formulas dir: %v", err)
	}

	// Create some formula files
	formulas := []string{
		constants.MolDeaconPatrol + ".formula.toml",
		constants.MolWitnessPatrol + ".formula.toml",
		"shiny.formula.toml",
	}
	for _, f := range formulas {
		path := filepath.Join(formulasDir, f)
		if err := os.WriteFile(path, []byte("# test"), 0644); err != nil {
			t.Fatalf("writing %s: %v", f, err)
		}
	}

	// Also create a non-formula file (should be ignored)
	if err := os.WriteFile(filepath.Join(formulasDir, ".installed.json"), []byte("{}"), 0644); err != nil {
		t.Fatalf("writing .installed.json: %v", err)
	}

	// Test
	names := getFormulaNames(tmpDir)
	if names == nil {
		t.Fatal("getFormulaNames returned nil")
	}

	expected := []string{constants.MolDeaconPatrol, constants.MolWitnessPatrol, "shiny"}
	for _, name := range expected {
		if !names[name] {
			t.Errorf("expected formula name %q not found", name)
		}
	}

	// Should not include the .installed.json file
	if names[".installed"] {
		t.Error(".installed should not be in formula names")
	}

	if len(names) != len(expected) {
		t.Errorf("got %d formula names, want %d", len(names), len(expected))
	}
}

func issueIDs(issues []*beads.Issue) []string {
	ids := make([]string, 0, len(issues))
	for _, issue := range issues {
		ids = append(ids, issue.ID)
	}
	return ids
}

func TestGetFormulaNames_NonexistentDir(t *testing.T) {
	names := getFormulaNames("/nonexistent/path")
	if names != nil {
		t.Error("expected nil for nonexistent directory")
	}
}

func TestFilterFormulaScaffolds(t *testing.T) {
	formulaNames := map[string]bool{
		constants.MolDeaconPatrol:  true,
		constants.MolWitnessPatrol: true,
	}

	issues := []*beads.Issue{
		{ID: constants.MolDeaconPatrol, Title: constants.MolDeaconPatrol},
		{ID: constants.MolDeaconPatrol + ".inbox-check", Title: "Handle callbacks"},
		{ID: constants.MolDeaconPatrol + ".health-scan", Title: "Check health"},
		{ID: constants.MolWitnessPatrol, Title: constants.MolWitnessPatrol},
		{ID: constants.MolWitnessPatrol + ".loop-or-exit", Title: "Loop or exit"},
		{ID: "hq-123", Title: "Real work item"},
		{ID: "hq-wisp-abc", Title: "Actual wisp"},
		{ID: "gt-456", Title: "Project issue"},
	}

	filtered := filterFormulaScaffolds(issues, formulaNames)

	// Should only have the non-scaffold issues
	if len(filtered) != 3 {
		t.Errorf("got %d filtered issues, want 3", len(filtered))
	}

	expectedIDs := map[string]bool{
		"hq-123":      true,
		"hq-wisp-abc": true,
		"gt-456":      true,
	}
	for _, issue := range filtered {
		if !expectedIDs[issue.ID] {
			t.Errorf("unexpected issue in filtered result: %s", issue.ID)
		}
	}
}

func TestFilterFormulaScaffolds_NilFormulaNames(t *testing.T) {
	issues := []*beads.Issue{
		{ID: "hq-123", Title: "Real work"},
		{ID: constants.MolDeaconPatrol, Title: "Would be filtered"},
	}

	// With nil formula names, should return all issues unchanged
	filtered := filterFormulaScaffolds(issues, nil)
	if len(filtered) != len(issues) {
		t.Errorf("got %d issues, want %d (nil formulaNames should return all)", len(filtered), len(issues))
	}
}

func TestFilterFormulaScaffolds_EmptyFormulaNames(t *testing.T) {
	issues := []*beads.Issue{
		{ID: "hq-123", Title: "Real work"},
		{ID: constants.MolDeaconPatrol, Title: "Would be filtered"},
	}

	// With empty formula names, should return all issues unchanged
	filtered := filterFormulaScaffolds(issues, map[string]bool{})
	if len(filtered) != len(issues) {
		t.Errorf("got %d issues, want %d (empty formulaNames should return all)", len(filtered), len(issues))
	}
}

func TestFilterFormulaScaffolds_EmptyIssues(t *testing.T) {
	formulaNames := map[string]bool{constants.MolDeaconPatrol: true}
	filtered := filterFormulaScaffolds([]*beads.Issue{}, formulaNames)
	if len(filtered) != 0 {
		t.Errorf("got %d issues, want 0", len(filtered))
	}
}

func TestGetWispIDsUsesBdMolWispList(t *testing.T) {
	beadsPath := t.TempDir()
	if err := os.WriteFile(filepath.Join(beadsPath, "issues.jsonl"), []byte(`{"id":"stale-jsonl-wisp"}`+"\n"), 0644); err != nil {
		t.Fatal(err)
	}

	binDir := t.TempDir()
	bdPath := filepath.Join(binDir, "bd")
	bdScript := `#!/bin/sh
if [ "$1" = "mol" ] && [ "$2" = "wisp" ] && [ "$3" = "list" ] && [ "$4" = "--json" ]; then
  printf '{"wisps":[{"id":"dolt-wisp-1"},{"id":"dolt-wisp-2"}],"count":2}\n'
  exit 0
fi
exit 1
`
	if err := os.WriteFile(bdPath, []byte(bdScript), 0755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	ids := getWispIDs(beadsPath)
	if !ids["dolt-wisp-1"] || !ids["dolt-wisp-2"] {
		t.Fatalf("expected IDs from bd mol wisp list, got %#v", ids)
	}
	if ids["stale-jsonl-wisp"] {
		t.Fatalf("getWispIDs read stale issues.jsonl; got %#v", ids)
	}
}

func TestFilterFormulaScaffolds_DotInNonScaffold(t *testing.T) {
	// Issue ID has a dot but prefix is not a formula name
	formulaNames := map[string]bool{constants.MolDeaconPatrol: true}

	issues := []*beads.Issue{
		{ID: "hq-cv.synthesis-step", Title: "Convoy synthesis"},
		{ID: "some.other.thing", Title: "Random dotted ID"},
	}

	filtered := filterFormulaScaffolds(issues, formulaNames)
	if len(filtered) != 2 {
		t.Errorf("got %d issues, want 2 (non-formula dots should not filter)", len(filtered))
	}
}

func TestFilterReadyIssuesByRoute(t *testing.T) {
	townRoot := t.TempDir()
	if err := os.MkdirAll(filepath.Join(townRoot, ".beads"), 0755); err != nil {
		t.Fatalf("creating town beads dir: %v", err)
	}
	routes := strings.Join([]string{
		`{"prefix":"hq-","path":"."}`,
		`{"prefix":"hq-cv-","path":"."}`,
		`{"prefix":"bds-","path":"bd_symphony/mayor/rig"}`,
		`{"prefix":"gt-","path":"gastown/mayor/rig"}`,
	}, "\n") + "\n"
	if err := os.WriteFile(filepath.Join(townRoot, ".beads", "routes.jsonl"), []byte(routes), 0644); err != nil {
		t.Fatalf("writing routes: %v", err)
	}

	issues := []*beads.Issue{
		{ID: "hq-123", Title: "town work"},
		{ID: "hq-cv-123", Title: "town convoy"},
		{ID: "bds-town-stale", Title: "wrongly-created town bds row"},
		{ID: "unknown-123", Title: "unknown route"},
	}
	filtered := filterReadyIssuesByRoute(townRoot, "town", issues)
	if got, want := issueIDs(filtered), []string{"hq-123", "hq-cv-123"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("town filtered IDs = %v, want %v", got, want)
	}

	issues = []*beads.Issue{
		{ID: "bds-123", Title: "bd_symphony work"},
		{ID: "hq-123", Title: "town work in rig result"},
		{ID: "gt-123", Title: "other rig work"},
		{ID: "unknown-123", Title: "unknown route"},
	}
	filtered = filterReadyIssuesByRoute(townRoot, "bd_symphony", issues)
	if got, want := issueIDs(filtered), []string{"bds-123"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("rig filtered IDs = %v, want %v", got, want)
	}
}

// TestRunReadyIncludesRigRootBeadsWithMetadataDatabase is a regression test for
// gt ready showing a rig-root .beads database as empty. bd was pinned to the
// rig's BEADS_DIR but not to the database named in .beads/metadata.json, so a
// stale/ambient BEADS_DOLT_SERVER_DATABASE (or bd's default) selected the town
// database. The rig source then reported only hq-* beads, which
// filterReadyIssuesByRoute drops, producing an empty-looking rig with no error.
func TestRunReadyIncludesRigRootBeadsWithMetadataDatabase(t *testing.T) {
	townRoot := t.TempDir()
	writeFile := func(path, content string) {
		t.Helper()
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0644); err != nil {
			t.Fatal(err)
		}
	}

	writeFile(filepath.Join(townRoot, "mayor", "town.json"), "{}")
	writeFile(filepath.Join(townRoot, ".beads", "routes.jsonl"),
		`{"prefix":"hq-","path":"."}`+"\n"+`{"prefix":"ti-","path":"tiroltourismus"}`+"\n")
	writeFile(filepath.Join(townRoot, "mayor", "rigs.json"),
		`{"version":1,"rigs":{"tiroltourismus":{"git_url":"x","added_at":"2026-01-01T00:00:00Z"}}}`)

	rigRoot := filepath.Join(townRoot, "tiroltourismus")
	writeFile(filepath.Join(rigRoot, ".beads", "metadata.json"),
		`{"backend":"dolt","dolt_database":"ti"}`)

	binDir := t.TempDir()
	bd := filepath.Join(binDir, "bd")
	script := `#!/bin/sh
if [ "$1" = "--allow-stale" ]; then
  case "$*" in
    *version*) printf 'bd 1.0.0\n'; exit 0;;
  esac
fi
case "$*" in
  *"mol wisp list"*) printf '{"wisps":[],"count":0}\n'; exit 0;;
  *ready*)
    if [ "${BEADS_DOLT_SERVER_DATABASE:-}" != "ti" ]; then
      printf '[{"id":"hq-999","title":"Wrong database","priority":2,"issue_type":"task"}]\n'
      exit 0
    fi
    # bd resolves .beads/metadata.json from cwd, so a rig query must run from
    # the rig root (parent of BEADS_DIR). Otherwise bd reads the town metadata
    # while the env selects the rig database and refuses with PROJECT IDENTITY
    # MISMATCH.
    if [ "$PWD" != "$(dirname "${BEADS_DIR:-}")" ]; then
      printf 'PROJECT IDENTITY MISMATCH: cwd=%s beads_dir=%s\n' "$PWD" "${BEADS_DIR:-}" >&2
      exit 1
    fi
    printf '[{"id":"ti-4ng","title":"Rig work","priority":2,"issue_type":"task"}]\n'
    exit 0;;
  *) printf '[]\n'; exit 0;;
esac
`
	if err := os.WriteFile(bd, []byte(script), 0755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))
	// Stale ambient selector that must not override the rig metadata database.
	t.Setenv("BEADS_DOLT_SERVER_DATABASE", "wrong")
	t.Setenv("GT_TOWN_ROOT", townRoot)
	beads.ResetBdAllowStaleCacheForTest()

	oldWD, _ := os.Getwd()
	if err := os.Chdir(townRoot); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = os.Chdir(oldWD) }()

	oldStdout := os.Stdout
	r, w, _ := os.Pipe()
	os.Stdout = w
	oldJSON, oldRig := readyJSON, readyRig
	readyJSON, readyRig = true, ""
	err := runReady(nil, nil)
	_ = w.Close()
	os.Stdout = oldStdout
	readyJSON, readyRig = oldJSON, oldRig
	if err != nil {
		t.Fatalf("runReady: %v", err)
	}

	var buf strings.Builder
	if _, err := io.Copy(&buf, r); err != nil {
		t.Fatal(err)
	}

	var res struct {
		Sources []struct {
			Name   string `json:"name"`
			Issues []struct {
				ID string `json:"id"`
			} `json:"issues"`
		} `json:"sources"`
	}
	if err := json.Unmarshal([]byte(buf.String()), &res); err != nil {
		t.Fatalf("unmarshal ready JSON: %v\n%s", err, buf.String())
	}

	var rigIDs []string
	for _, src := range res.Sources {
		if src.Name != "tiroltourismus" {
			continue
		}
		for _, issue := range src.Issues {
			rigIDs = append(rigIDs, issue.ID)
		}
	}
	if len(rigIDs) != 1 || rigIDs[0] != "ti-4ng" {
		t.Fatalf("rig ready IDs = %v, want [ti-4ng]\noutput: %s", rigIDs, buf.String())
	}
}
