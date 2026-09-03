# Repository issue manager

This is a maintained, ready-to-use Skillz artifact. Preserve its dynamic Express/Vite runtime and the `/files` gateway. It manages schema-version-2 issue records in `repo_facts.md` for explicitly granted repositories.

Each root reports `read` or `write` access. Never present mutation controls for a read-only root. All saves must call `files.writeText` with the SHA-256 of the exact loaded content. On a conflict, leave local state unchanged and ask the user to refresh. Do not add direct host paths, unrestricted filesystem endpoints, shell endpoints, or ways to change grants from the artifact.

Keep unknown ledger fields intact. Preserve the Markdown surrounding the fenced JSON object and only modify issue lifecycle fields needed for the selected action. Run `npm run build` after changes.
