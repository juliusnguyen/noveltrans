Ship the current feature: commit, update changelog, (optionally bump version), open PR, merge, return to main.

**Steps to execute:**

1. Confirm the current branch is a `changes/NNN-FEATURE-NAME` branch (also accept `feature/NNN-FEATURE-NAME` for compatibility). If not, warn and stop.

2. Check for uncommitted changes — stage and commit any outstanding work first with a short "wip: final cleanup" message if needed, or remind the user to commit first.

3. **Bump the patch version (optional — only if a `VERSION` file exists at the repo root)**:
   - Read `VERSION`, increment the patch number (e.g. `0.1.0` → `0.1.1`), write it back. Use `make bump` if available.
   - If no `VERSION` file exists, skip this step silently — this project does not use a top-level VERSION file.

4. **Update CHANGELOG.md**:
   - Read `changes/NNN-FEATURE-NAME/NNN.01-HISTORY.md` and any plan files for a summary of what was done
   - Add a new section dated today summarising the feature. If VERSION was bumped, title it `## [VERSION] — YYYY-MM-DD — FEATURE-NAME`; otherwise `## YYYY-MM-DD — NNN-FEATURE-NAME`.
   - Insert it above any existing `[Unreleased]` section (if present). Keep `[Unreleased]` empty and ready for the next feature.
   - If `CHANGELOG.md` does not exist, skip this step (don't create one unless the user asks).

5. **Commit everything**:
   ```
   git add -A
   git commit -m "feat(NNN-FEATURE-NAME): <one-line summary from history>"
   ```
   Use a HEREDOC if the message has multiple lines. Co-author trailer (per the project's commit style) is optional — follow the pattern in recent `git log` entries.

6. **Create a PR** using `gh pr create` with:
   - Title: `feat(NNN-FEATURE-NAME): <summary>` (under 70 chars). Append `(vVERSION)` only if VERSION was bumped.
   - Body: summary from the history file + plan references, followed by a `## Test plan` checklist
   - Target: `main`

7. **Merge the PR**:
   ```
   gh pr merge --merge --delete-branch
   ```

8. **Return to main and pull**:
   ```
   git checkout main
   git pull
   ```

9. Report the PR URL, the new version (if bumped), and that the feature branch has been deleted.
