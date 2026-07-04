Start a new feature. Usage: `/feature FEATURE-NAME` or `/feature` (will prompt for name).

**Steps to execute:**

1. Determine the feature name from the arguments provided, or ask the user if none given.

2. Scan `changes/` for existing `NNN-*` directories. Take the highest NNN, add 1, zero-pad to 3 digits (e.g. `035` → `036`). If no directories exist, start at `001`.

3. Construct the feature slug: `NNN-FEATURE-NAME` (uppercase, hyphens). E.g. `036-MY-FEATURE`.

4. Create the directory: `changes/NNN-FEATURE-NAME/`

5. Write the feature prompt to `changes/NNN-FEATURE-NAME/NNN.00-PROMPT.md`. If the user included a description in their `/feature` message, use that as the content. Otherwise write a short stub and ask the user to fill it in.

6. Create and switch to the branch (this project uses the `changes/` prefix, not `feature/`):
   ```
   git checkout -b changes/NNN-FEATURE-NAME
   ```

7. Report: show the branch name, the prompt file path, and remind the user that:
   - `/plan` will generate and save an implementation plan
   - All work will be logged to `changes/NNN-FEATURE-NAME/NNN.01-HISTORY.md` (the Stop hook blocks finishing a turn if source files changed but this file wasn't updated)
   - `/ship` will close and merge when done
