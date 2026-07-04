Generate an implementation plan for the current feature and save it to the changes directory.

**Steps to execute:**

1. Detect the current feature from the git branch name. The branch should be `changes/NNN-FEATURE-NAME` (this project's convention; also accept `feature/NNN-FEATURE-NAME` for compatibility). Parse out `NNN` and `FEATURE-NAME`. If not on a matching branch, ask the user which feature this plan is for.

2. Read `changes/NNN-FEATURE-NAME/NNN.00-PROMPT.md` for context. Also read any existing plan files (`NNN.MM-*.md`) to understand prior planning.

3. Determine the next MM number for plans: scan for files matching `NNN.MM-*.md` in the feature dir (excluding PROMPT and HISTORY files). Take the highest MM found and add 1. If none, MM = `01`.

4. Determine the plan name: use any name provided after `/plan`, otherwise derive a short slug from the feature name (e.g. `INITIAL-PLAN`). Convert to UPPERCASE and hyphenate spaces. Format: `NNN.MM-UPPERCASE-PLAN-TITLE.md` (e.g. `006.03-KEYBINDINGS-PLAN.md`).

5. Use the **Plan subagent** (`subagent_type: Plan`) to think through the implementation. Pass it:
   - The feature prompt content
   - The current codebase context (relevant files)
   - Any specific focus from the user's message
   The Plan agent will return a detailed step-by-step plan.

6. Write the plan output to `changes/NNN-FEATURE-NAME/NNN.MM-UPPERCASE-PLAN-TITLE.md`.

7. Append a log entry to `changes/NNN-FEATURE-NAME/NNN.01-HISTORY.md` (create if needed):
   ```
   ## [PLAN] NNN.MM-UPPERCASE-PLAN-TITLE — <date>
   Generated plan: `NNN.MM-UPPERCASE-PLAN-TITLE.md`
   ```

8. Show the plan to the user and confirm readiness to implement.
