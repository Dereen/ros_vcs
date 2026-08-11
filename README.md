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
- **Colcon config**: `colcon_defaults.yaml` / `.colcon/config.yaml` travel with the export, so a workspace pins its own build settings instead of every importer passing them by hand
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

#### System dependencies

A restored workspace is source only — it still needs whatever its packages
declare in `package.xml`. Every import ends with a read-only `rosdep check`, so
what stands between the import and a successful build is visible before you
invoke colcon:

```
Warning: 7 system dependency(ies) not installed:
  [apt] libgoogle-glog-dev ros-jazzy-grid-map-core ros-jazzy-grid-map-ros ...
  Install with: rvcs --import-state ... --install-deps

Warning: 5 rosdep key(s) could not be resolved:
  elevation_mapping: pcl
  marv_xu_hto: marv_flipper_baselines
  These are unknown to rosdep -- typically a wrong key in the
  package's package.xml, or a repo missing from the pipeline.
```

The second group is the easy one to miss: `rosdep install -r` prints those and
carries on, so an unresolvable key looks like success until the build fails.

`--install-deps` runs the install for you:

```bash
./rvcs.py --import-state ws.pipeline.zip ~/new_ws --install-deps
```

It is opt-in because rosdep shells out to `sudo -H apt-get` and will prompt for
a password — a plain `--import-state` stays non-interactive and never changes
the machine outside the target directory. rosdep must have been initialised
(`rosdep init` + `rosdep update`); if its cache is empty, rvcs says so rather
than failing obscurely. No ROS overlay needs to be sourced — the distro is taken
from `$ROS_DISTRO`, falling back to what is installed under `/opt/ros`.

#### Building

`--build` runs `colcon build --symlink-install` once everything is restored and
dependencies are in place, so one command takes a zip to a built workspace:

```bash
./rvcs.py --import-state ws.pipeline.zip ~/new_ws --install-deps --build
```

colcon resolves ament packages out of the environment, and rvcs runs from its
own venv with no ROS overlay sourced — so the build goes through a shell that
sources `/opt/ros/<distro>/setup.bash` first, with the distro taken from
`$ROS_DISTRO` or `/opt/ros`.

Extra colcon arguments go through `--build-args`, environment through
`--build-env` (repeatable):

```bash
./rvcs.py --import-state ws.pipeline.zip ~/new_ws --build \
    --build-args "--continue-on-error --cmake-args -DBUILD_TESTING=OFF" \
    --build-env CMAKE_BUILD_PARALLEL_LEVEL=8
```

**CUDA**: toolkits install into `/usr/local/cuda*/bin`, which distros do not put
on `PATH`, so a package with a CUDA target fails at configure time with
`No CMAKE_CUDA_COMPILER could be found` even though nvcc is installed. rvcs
detects this and prepends the directory, reporting what it did:

```
env: PATH += /usr/local/cuda/bin (nvcc found but not on PATH)
```

Target GPU architecture is not guessed — pass it yourself when a package
hardcodes one that does not match the local card, e.g.
`--build-args "--cmake-args -DCMAKE_CUDA_ARCHITECTURES=86"`.

The build is opt-in: it is slow, and it writes `build/`, `install/` and `log/`
into the target directory.

**Workspace build settings belong in the workspace, not in the command.** colcon
reads `colcon_defaults.yaml` from the directory it runs in, and exports carry
that file (along with `.colcon/config.yaml`) — neither lives inside a repo, so
nothing else in the zip would preserve them. A workspace that pins its own
settings therefore needs no `--build-args` at all:

```yaml
# <workspace>/colcon_defaults.yaml
build:
  cmake-args:
    - -DBUILD_TESTING=OFF
```

```bash
rvcs --import-state ws.pipeline.zip ~/new_ws --install-deps --build   # no flags
```

Prefer this over `--build-args` for anything intrinsic to the workspace: it is
recorded once, travels with every export, and cannot be forgotten on the next
machine.

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

The comparison is **content-aware**: a zip-side change you already have —
its dirty patch committed here since the export, or its untracked file now
tracked locally with identical content — shows as `=` (`zip change already
in local` / `already in local, tracked`) instead of a false `+`. A zip
untracked file that exists here with *different* content shows the real
diff (`tracked locally, content differs`).

The tree is also a MERGE tool — every action applies to the selected node
*and everything beneath it*, so it works from a single file up to a whole repo:

| Key | Action |
|-----|--------|
| `d` | preview the 3-way merge of the selected file (conflict markers shown, nothing written) |
| `o` | accept **ours** — keep the local side |
| `t` | accept **theirs** — make the local file(s) match the zip (after y/N confirm) |
| `m` | 3-way **merge** (base = local HEAD): non-overlapping changes combine, overlaps get `<<<<<<< local` / `>>>>>>> zip` conflict markers written into the file for manual resolution |
| `M` | merge **all** — the whole tree |
| `u` | safe batch **update** — apply every non-conflicting zip change (`--update-state` semantics: conflicting files stay untouched and are listed), then walk the leftovers with `o`/`t`/`m`. Applied items stay marked `✓ (applied by update)` across reloads; once nothing differs any more the root node turns `✓ … (fully in sync)` |
| `r` | reload the diff from disk (also happens automatically after every action) |

Resolved nodes turn `✓`, conflicted ones `!`. Before the first modification of
any file its original is copied to `/tmp/rvcs_merge_backup_<timestamp>/`, and
the backup path is shown in the root node after every action.

Commit divergence is handled on the **repo** node: when the zip's commits
exist only in its `bundles/`, `t` first *fetches* the bundle (refs only,
under `refs/rvcs-bundle/`, working tree untouched); after the automatic
reload `t` *merges* the now-visible commits — fast-forward when the local
repo is strictly behind, a real merge commit when diverged, and a clean
abort with a hint when the merge conflicts. Zip-only commits without a
bundle live on the repo's remote — `git fetch` there, then `r`.

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
| `--install-deps` | With --import-state: run `rosdep install` for the restored workspace (needs sudo) |
| `--build` | With --import-state: run `colcon build` on the restored workspace |
| `--build-args ARGS` | Extra arguments for `--build`, e.g. `"--cmake-args -DBUILD_TESTING=OFF"` |
| `--build-env KEY=VALUE` | Environment override for `--build` (repeatable) |
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
