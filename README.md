![Claude Code Opus 4.5 coded | Unreviewed](https://img.shields.io/badge/Claude%20Code%20Opus%204.5%20coded-Unreviewed-grey?logo=claude&logoColor=white&labelColor=D97757)

# RVCS - ROS VCS Workspace Manager

A command-line tool for managing ROS/catkin workspaces with multiple git repositories. Export and import complete workspace state including uncommitted changes.

## Features

- **Status Overview**: Color-coded table showing branch, commit, and dirty state for all repositories
- **Workspace Export**: Export entire workspace to a single zip file (vcstool format + uncommitted changes)
- **Workspace Import**: Recreate workspace from zip file, including applying uncommitted changes
- **Comparison Mode**: Compare local workspace against a remote snapshot
- **vcstool Compatible**: Export format is compatible with standard `vcs import` command
- **Pipelines**: Back up one *slice* of a shared workspace (the repos one line of research uses) together with its tmuxinator session configs
- **Git-bundle fallback**: Repos whose HEAD no remote can serve (unpushed commits, no remote, or a configured remote that doesn't exist) are embedded in the zip as git bundles, so exports are restorable without pushing first

## Installation

```bash
pip install -r requirements.txt
```

Or install dependencies individually:

```bash
pip install GitPython PyYAML tabulate vcstool
```

## Usage

### Show Workspace Status

```bash
# Current directory (auto-detects catkin workspace)
./rvcs.py

# Specific workspace
./rvcs.py ~/catkin_ws
```

Output shows a color-coded table:
- **Green**: Clean repository
- **Orange**: Repository with uncommitted changes

### Export Workspace State

Export workspace to a single zip file containing:
- `workspace.repos` - vcstool YAML format with exact commit hashes
- `workspace.state.yaml` - Diffs for repositories with uncommitted changes

```bash
./rvcs.py --export-state ~/catkin_ws
# Creates: catkin_ws_2024-01-22_12-30-45.workspace.zip
```

Repos whose HEAD commit is not reachable from any remote are additionally saved as
`bundles/<repo>.bundle` inside the zip: an *incremental* bundle (unpushed commits only)
when the repo has remote-tracking refs, otherwise a *self-contained* bundle with the
full history (covers repos with no remote, or a remote that was never created). Import
restores these automatically — incremental bundles clone the base from the remote and
fetch the missing commits from the bundle; self-contained bundles need no remote at all.

### Import Workspace State

Recreate workspace from exported zip (includes uncommitted changes):

```bash
./rvcs.py --import-state workspace.workspace.zip ~/new_workspace
```

Import from vcstool `.repos` file only (no uncommitted changes):

```bash
./rvcs.py --import-state workspace.repos ~/new_workspace
```

Import with separate state file:

```bash
./rvcs.py --import-state workspace.repos --state-file workspace.state.yaml ~/new_workspace
```

### Pipelines

A large workspace usually hosts several independent lines of work ("pipelines") that
share infrastructure repos. A **pipeline definition file** (`*.pipeline.yaml`) names the
subset of the workspace one pipeline uses, plus the tmuxinator configs that bring it up:

```yaml
name: overhang_rejection            # required — used for the output filename
workspace: ~/marv_ws                # optional default workspace
repos:                              # paths relative to src/ — subtree semantics,
  - overhang_research               #   nested repos inside an entry are included too
  - robot_rodeo_gym_ros2
tmuxinator:                         # session configs bundled verbatim
  - ~/.config/tmuxinator/marv_overhang.yml
extra_paths:                        # optional NON-repo paths (workspace-relative)
  - src/marv_elevation_mapping      #   copied into the zip as raw files
```

Keep the definition file inside the pipeline's main repo so it is versioned with the
research it describes.

Export a pipeline (creates `<name>_<date>.pipeline.zip`):

```bash
./rvcs.py --export-pipeline overhang.pipeline.yaml            # workspace from the file
./rvcs.py --export-pipeline overhang.pipeline.yaml ~/other_ws # explicit workspace
```

The zip contains the usual `workspace.repos` + `workspace.state.yaml` (restricted to the
pipeline's repos, uncommitted changes included), plus `pipeline.yaml` (the definition),
`tmuxinator/<name>.yml`, and `extra/<path>` raw files.

Status of only the pipeline's repos:

```bash
./rvcs.py --pipeline overhang.pipeline.yaml
```

Import works with the standard command; tmuxinator configs are restored to
`<workspace>/tmuxinator/` (add `--install-tmuxinator` to also copy them into
`~/.config/tmuxinator/`), and `extra_paths` are restored to their original
workspace-relative locations:

```bash
./rvcs.py --import-state overhang_rejection_2026-07-29.pipeline.zip ~/new_ws --install-tmuxinator
```

#### Path portability

Tmuxinator configs and pipeline definitions routinely hardcode the workspace
they were written for (`root: ~/marv_ws`, `bash /home/alice/marv_ws/sim/run.sh`).
Restoring those verbatim on another machine — or into a different directory —
breaks every window.

Exports record the workspace root in `workspace.state.yaml`, and import rewrites
it to wherever you actually imported, reporting each substitution:

```
Rewrote 10 path(s) in tmuxinator/marv_flipper_eval.yml: /home/alice/marv_ws -> /home/bob/workspaces/marv_ws
```

Zips written before this existed have no recorded root; import infers it from
the payload instead (`Inferred export-time workspace root: ...`). Use
`--no-path-rewrite` to restore the payload byte-for-byte.

Only files rvcs itself wrote into the zip are rewritten — `pipeline.yaml` and
`tmuxinator/*`. Repository working trees and `extra_paths` content are never
touched: they are tracked git content, and rewriting them would show up as
phantom diffs. Paths hardcoded *inside* the repos are reported after import
instead, so you can fix them upstream:

```
Warning: 62 reference(s) to paths that do not exist on this machine:
  /home/alice* — 62 reference(s) in 29 file(s)
      src/marv_flipper_control_research/marv_flipper_eval/launch/policy.launch.py
      ...
```

### Diff an export against a live workspace (TUI)

When a collaborator sends their exported state back, browse what changed before
deciding what to take:

```bash
./rvcs.py --diff-state flipper_eval_marv_2026-08-05.workspace.zip ~/marv_ws
./rvcs.py --diff-state export.zip --pipeline flipper_eval.pipeline.yaml   # restrict to a pipeline
./rvcs.py --diff-state export.zip ~/marv_ws --no-tui                      # plain tree to stdout
```

On a terminal this opens a curses tree (detail pane on the right): repos marked
`+` only-in-zip / `-` only-local / `~` differing / `=` in sync; a differing repo
expands into per-file nodes whose detail shows both sides' patches, untracked
file diffs, and how the commits relate (ahead/behind with the commit list, or a
note when the zip HEAD only exists on the other machine — check its `bundles/`).
tmuxinator configs and the colcon config are compared too; repos that exist
locally but not in the zip are summarized, not flagged. Keys: `j/k` move,
`l`/`Enter` expand, `h` collapse (again: jump to parent), `J/K`/`PgUp/PgDn`
scroll the detail pane, `g/G` top/bottom, `q` quit.

The tree is also a MERGE tool — every action applies to the selected node
*and everything beneath it*, so it works from a single file up to a whole repo:

| Key | Action |
|-----|--------|
| `d` | preview the 3-way merge of the selected file (conflict markers shown, nothing written) |
| `o` | accept **ours** — keep the local side |
| `t` | accept **theirs** — make the local file(s) match the zip (after y/N confirm) |
| `m` | 3-way **merge** (base = local HEAD): non-overlapping changes combine, overlaps get `<<<<<<< local` / `>>>>>>> zip` conflict markers written into the file for manual resolution |
| `M` | merge **all** — the whole tree |

Resolved nodes turn `✓`, conflicted ones `!`. Before the first modification of
any file its original is copied to `/tmp/rvcs_merge_backup_<timestamp>/`, and
the backup path is shown in the root node after every action. Commit
divergence is reported but never merged automatically — fetch the zip's
`bundles/` for that.

For scripts and AI tooling (e.g. Claude Code), dump the same tree as JSON
instead of opening the TUI:

```bash
./rvcs.py --diff-state export.zip ~/marv_ws --diff-json -          # stdout
./rvcs.py --diff-state export.zip ~/marv_ws --diff-json diff.json  # file
```

### Update the workspace from an export (non-conflicting changes only)

The non-interactive companion to the TUI: apply everything that merges
cleanly, refuse everything that doesn't.

```bash
./rvcs.py --update-state export.zip ~/marv_ws --dry-run   # report only
./rvcs.py --update-state export.zip ~/marv_ws             # apply
```

Zip-side changes are taken via a true 3-way merge (when the zip HEAD contains
commits unknown locally, the zip's `bundles/` are fetched — objects only — so
patches apply onto the base they were made against). A file is written only
when the merge has zero conflicts and actually changes it. Never touched:
files where the merge would conflict, files already carrying conflict markers,
locally-tracked files the zip only has as untracked, binary divergence, and
all local-only changes. Each skip is reported with its reason and a pointer to
the `--diff-state` TUI; originals of everything written go to
`/tmp/rvcs_merge_backup_<timestamp>/`.

### Compare Workspaces

Compare local workspace against a JSON snapshot:

```bash
./rvcs.py -c remote_snapshot.json ~/catkin_ws
```

Color codes:
- **Green**: Matching and clean
- **Yellow**: Different branch or commit
- **Orange**: One side has uncommitted changes
- **Red**: Both sides have uncommitted changes
- **Blue**: Local only
- **Cyan**: Remote only
- **Magenta**: Same package, different URL

### Export to JSON (Legacy)

```bash
./rvcs.py -j ~/catkin_ws
# Creates: catkin_ws_2024-01-22_12-30-45.json
```

## Options

| Option | Description |
|--------|-------------|
| `--export-state` | Export workspace to zip file |
| `--export-pipeline FILE` | Export a pipeline slice (repos + tmuxinator configs) to zip |
| `--pipeline FILE` | Restrict status/compare to a pipeline's repos |
| `--import-state FILE` | Import from .workspace.zip, .pipeline.zip or .repos file |
| `--state-file FILE` | State file with diffs (use with --import-state) |
| `--install-tmuxinator` | With --import-state: copy bundled tmuxinator configs to ~/.config/tmuxinator |
| `--no-path-rewrite` | With --import-state: restore the pipeline payload verbatim (skip root rewriting) |
| `-c, --compare FILE` | Compare with JSON snapshot |
| `-j, --json` | Export to JSON file |
| `--ignore FILE` | File with package names to exclude |
| `-d, --debug` | Enable debug output |
| `--version` | Show version |

## Zip File Contents

The exported `.workspace.zip` contains:

### workspace.repos
vcstool-compatible YAML format:
```yaml
repositories:
  my_package:
    type: git
    url: git@github.com:user/my_package.git
    version: abc1234def5678
```

### workspace.state.yaml
State information including diffs:
```yaml
workspace_name: catkin_ws
export_date: "2024-01-22_12-30-45"
dirty_repos:
  my_package:
    staged_diff: |
      diff --git a/file.py b/file.py
      ...
    unstaged_diff: |
      diff --git a/other.py b/other.py
      ...
    untracked_files:
      new_file.txt:
        content: "file contents here"
        binary: false
```

## Library Usage

```python
from rvcs import export_workspace_state, import_workspace_state, scan_workspace

# Scan workspace
results = scan_workspace('/path/to/workspace')
for repo in results:
    print(f"{repo['package']}: {repo['branch']} ({repo['hash']})")

# Export workspace state
zip_path = export_workspace_state('/path/to/workspace')

# Export a pipeline slice (repos subset + tmuxinator configs)
from rvcs import export_pipeline_state
zip_path = export_pipeline_state('overhang.pipeline.yaml')

# Import workspace state
import_workspace_state('workspace.workspace.zip', '/path/to/new/workspace')
```

## Requirements

- Python 3.8+
- Git
- Dependencies: GitPython, PyYAML, tabulate, vcstool

## License

MIT License
