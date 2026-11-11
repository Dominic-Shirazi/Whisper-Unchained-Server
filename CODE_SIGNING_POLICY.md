# Code signing policy

Windows installers for Whisper Unchained Server are built by GitHub Actions from
this public repository ([build workflow](.github/workflows/build-installer.yml))
and, once approved, signed through [SignPath.io](https://signpath.io) with a
certificate issued by the [SignPath Foundation](https://signpath.org).

## Roles

| Role | Who |
| :--- | :--- |
| Committers and reviewers | [Dominic-Shirazi](https://github.com/Dominic-Shirazi) |
| Approvers (approve each signing request) | [Dominic-Shirazi](https://github.com/Dominic-Shirazi) |

All of them use multi-factor authentication on GitHub and SignPath.

## What gets signed

Only the installer built from a tagged commit of this repository by the
workflow above. Nothing built on a personal PC is signed.

## Privacy

This program doesn't send any information anywhere unless you set it up to:
transcription runs on your own PC, or on another PC or cloud service that you
choose and configure yourself. It never sends usage data. See the
[README](README.md) for what each setting sends where.
