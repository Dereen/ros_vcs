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
