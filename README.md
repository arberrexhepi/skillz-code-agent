# Repository issue manager

A ready-to-use Skillz artifact for managing the schema-version-2 issue ledger in `repo_facts.md` across explicitly shared repositories. It can initialize the ledger, create issues, change the active issue, close issues, and reopen them.

The app only offers mutations for roots granted **Allow changes**. Saves include the SHA-256 of the loaded file, so a concurrent agent/editor update is rejected and must be refreshed rather than overwritten.
