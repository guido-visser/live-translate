# Code signing with SignPath

Release builds (a `v*` tag) are signed through the free [SignPath Foundation](https://signpath.org) programme for open-source projects. Unsigned installers are blocked by Smart App Control on Windows 11 ("An Application Control policy has blocked this file", error 4551).

## Why two signing requests per release

Setup copies itself to the temp folder and runs from there. Signing only the finished installer leaves that copy unsigned, and Smart App Control blocks it ("Unable to execute file in the temporary directory"). So [build.yml](../../../.github/workflows/build.yml) does this:

1. Inno Setup writes its uninstaller (`uninst-<inno version>-<hash>.e32`) and stops (`build.ps1 -UninstallerOnly`).
2. SignPath signs it (artifact configuration `uninstaller`).
3. Inno Setup builds the installer with that signed file (`SignedUninstaller=yes`). The uninstaller and Setup's temp copy carry the signature.
4. SignPath signs the installer itself (artifact configuration `installer`).

Each release therefore asks for **two approvals** in SignPath: first the uninstaller, then the installer. The workflow waits up to an hour for each.

## One-time setup

1. Apply at [signpath.org/apply](https://signpath.org/apply) with this repository. The README already has the required **Code signing policy** and **Privacy** sections, the repository has the MIT `LICENSE`, and the privacy policy the form asks for is [PRIVACY.md](../../../PRIVACY.md) (link: `https://github.com/guido-visser/live-translate/blob/main/PRIVACY.md`, which works once this branch is merged).
2. Once accepted, SignPath sets up the organisation and the GitHub connector with you. Turn on two-factor authentication for both SignPath and GitHub (required).
3. In SignPath, make sure there is:
   * a project with slug `live-translate`, linked to this GitHub repository;
   * a signing policy with slug `release-signing` (the Foundation's release certificate, with manual approval);
   * two artifact configurations, with slugs `uninstaller` and `installer`, holding [uninstaller.xml](uninstaller.xml) and [installer.xml](installer.xml).

   Other slugs are fine too; then change `SIGNPATH_PROJECT` / `SIGNPATH_POLICY` at the top of build.yml.
4. In SignPath, create an API token for a CI user that may submit to that policy.
5. In GitHub, under **Settings → Secrets and variables → Actions**:
   * secret `SIGNPATH_API_TOKEN`: that token;
   * variable `SIGNPATH_ORGANIZATION_ID`: the organisation ID from SignPath.

## Releasing

Push a tag as before (`v0.1.15`). Approve both signing requests in SignPath when they appear. The release on GitHub then gets the signed installer, and the in-app updater installs that.

To check a downloaded installer: right-click it → **Properties → Digital Signatures**. It should say *SignPath Foundation*.

Pushes to `main` and pull requests still build an unsigned installer, as before.
