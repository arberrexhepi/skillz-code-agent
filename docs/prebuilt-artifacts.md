# Cloning and publishing the prebuilt artifacts

The bundled Server Manager and Repository issue manager are Git submodules. Their source histories live on the independent branches `prebuilt/server-manager` and `prebuilt/repo-issue-manager` in the same remote repository as the application. The application stores their exact commit IDs. A normal push of an application branch does not upload those separate histories.

Both entries in `.gitmodules` use `url = ./`, so a fresh recursive clone gets the prebuilts from the repository being cloned. It does not depend on the original owner's GitHub account. Git resolves this relative URL against the current branch's upstream remote, or `origin` when no upstream is configured.

Clone with the prebuilts included:

```bash
git clone --recurse-submodules https://github.com/arberrexhepi/skillz-code-agent.git
cd skillz-code-agent
npm --prefix desktop install
```

After pulling application changes, restore the recorded versions:

```bash
git pull
npm --prefix desktop run submodules:init
```

The initialization command synchronizes cached submodule URLs before checking out the pinned commits. Development and build commands check that both prebuilt source repositories and their required files exist; missing checkouts stop with an initialization instruction. `submodules:update` is an explicit upgrade to the latest configured prebuilt branches, not a routine pull step.

To publish an independent copy to your own GitHub account, create an empty repository there. Initialize the prebuilts from the original repository **before** changing remotes, then keep the original as `upstream` and add your own destination:

```bash
git remote rename origin upstream
git remote add origin https://github.com/YOUR-ACCOUNT/YOUR-REPOSITORY.git
npm --prefix desktop run repo:publish -- origin
```

For a separately named repository, update the root README clone URL and GitHub workflow badge URL to point to that repository.

Use `repo:publish` for subsequent application pushes too. The remote name defaults to `origin`; another configured remote can be selected explicitly. The command:

- Requires the relative URLs and submodule pointers to be committed, and both prebuilt working trees to be clean and at those recorded commits.
- Uploads the required prebuilt histories to the selected destination's `prebuilt/*` branches, using an atomic, non-forced push for those branches.
- Fetches those branches from that exact destination and verifies that they contain every current pinned commit. Availability on the original remote or in a local cache is insufficient.
- Pushes the current application branch only after verification succeeds, then configures that branch's upstream to the selected remote. An already-newer prebuilt branch containing the pin is preserved; divergent histories or rejected prebuilt pushes stop publication.

When editing a prebuilt, create a working branch inside its submodule first: recursive clones normally leave submodules at detached HEAD. Commit there, then commit the updated pointer in the application:

```bash
git -C desktop/prebuilt-artifacts/server-manager switch -c feature/server-manager
# Edit and validate the prebuilt, then stage its intended files.
git -C desktop/prebuilt-artifacts/server-manager commit -m "Update Server Manager"
git add desktop/prebuilt-artifacts/server-manager
git commit -m "Update bundled Server Manager"
npm --prefix desktop run repo:publish -- origin
```

Keep these artifact source histories separate from the `app` → `dev` → `main` application promotion flow. The publisher does not create commits, merge the source branches into the application, or publish unrelated local branches.

On another machine, recursively clone **your destination repository**. For later pulls, run `submodules:init` again. To check destination availability without pushing:

```bash
npm --prefix desktop run submodules:verify -- origin
npm --prefix desktop run test:prebuilts
```

The **Prebuilt artifacts** GitHub Actions workflow checks out application branches recursively, verifies pins against the checkout's own repository, and tests independent publishing plus a second-machine clone/pull. Pull requests are checked against the contributor's repository. Ordinary `git push` bypasses the publisher, so use this workflow as a required check where branch protection is enabled.

This workflow covers bundled prebuilt **sources**. Copies installed through the desktop into a user's artifact library remain independent repositories with their own publishing configuration.
