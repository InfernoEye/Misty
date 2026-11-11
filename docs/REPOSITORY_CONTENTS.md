# What this private GitHub repository contains

Repository: https://github.com/InfernoEye/Misty

This is a portable development snapshot, **not a byte-for-byte backup of the 12 GB local workspace**. Source editing on another PC or through a connected coding assistant does not require copying installed environments and model weights into Git.

## Included

- Current application source, tests, tools, scripts, project configuration without secrets, and build definitions.
- Design documents, development/audit notes and reviewed historical source snapshots/backups. Older snapshots are historical, not the current entry point or a claim of tested compatibility.
- The optional, gated Cua integration and local visual decision-runtime source. Autonomous Cua input has not passed host acceptance and is not enabled by publication.
- Exact upstream commits as Git submodules for llama.cpp and whisper.cpp. Both native working trees were clean when the initial repository was prepared.
- Dependency pins, the historical environment/model manifest, and cross-PC setup instructions.

## Kept local / restored separately

| Content | Reason and restoration |
|---|---|
| Model weights (about 8.3 GiB) | Too large for ordinary Git; restore the exact four model files and verify hashes in [MANIFEST.lock](../MANIFEST.lock). They were **not** uploaded via Git LFS, release assets or another service. |
| Python environment (about 773 MiB) | Machine-specific installed packages; recreate from the dependency files. |
| Installed OCR/browser tools and caches under local data (about 858 MiB total) | Reinstall for the destination OS. Do not copy a virtual environment between PCs. |
| Native build outputs and dependency Git object databases (part of about 1.1 GiB) | Fetch pinned submodules and rebuild. |
| Runtime databases, logs, screenshots and recorded audio | May contain personal conversations, captured screens and other private information; these are not a source dependency. |
| Local SearXNG secret configuration | A real local secret was found and excluded before the initial upload. Use [the example](../config/searxng/settings.example.yml) and generate a new secret on each installation. |
| Opaque local archives, including the old baseline archive | Not text-audited; may duplicate secrets or runtime history. Reviewed source is tracked directly instead. |
| Temporary/editor/process caches | Generated local state, not necessary for development. |

[.gitignore](../.gitignore) is the authoritative exclusion list. No local files were deleted to prepare the repository. If you need an exact machine backup, use a separate encrypted backup mechanism; GitHub publication is not a backup of the excluded content. A private repository should still not carry authentication credentials or personal runtime history.

## Restoring and collaborating

Start with [cross-PC setup](CROSS_PC_SETUP.md). Use `git clone --recurse-submodules` to include native source at its pinned commits. Models still require a separate trusted transfer/acquisition; do not assume filenames establish the correct model revision or license.

Private GitHub access from the ChatGPT mobile app requires a supported GitHub connection and authorization to this repository. Repository creation alone does not grant ChatGPT access, execution or write permissions. Available connector/coding features determine whether a mobile conversation can inspect code, propose changes or open pull requests. Review changes before merging; another PC should pull the merged branch and recreate its local runtime.

## Initial verification

Before the initial publication, full unittest discovery ran 2,207 tests successfully (one skipped). The suite uses fakes for desktop/model behavior; it does not establish live Cua/KDE Wayland input support, real model grounding or full cross-PC provisioning. Resource-cleanup warnings remain in some tests.
