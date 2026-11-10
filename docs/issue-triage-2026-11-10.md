# Open issue triage — 2026-11-10

Reviewed all 30 open issues and their comments against `main` at `ea44087`.
The working tree was clean and matched `origin/main` before this review.

| Issue | Assessment | Resolution |
| --- | --- | --- |
| #47 | The documentation is stale; restoring permissive initialization is unsafe. | Keep the registration/topology/health guards. Explain concurrent registration failures and explicit recovery; close the requested behavior as not planned. |
| #48 | Valid. Empty or absent IDs bypassed UUID generation. | Regenerate deterministically for empty IDs and preserve existing nonempty IDs. |
| #49 | Valid. Restart timing comments differed from the unit. | Document 30 seconds and allow about 120 seconds for mount activation. |
| #50 | Valid. Missing controller binaries failed after host changes. | Add a shared controller preflight before deployment and upgrade mutations. |
| #51 | Valid configurability/documentation gap. | Expose OOMScoreAdjust with default 0 and document why immunity is not the default. |
| #52 | Valid. Text output rendered Markdown. | Implement a plain text renderer and exercise the CLI format selection. |
| #53 | Valid. Linger always changed and ignored errors. | Inspect persistent linger state and propagate enable failures. |
| #54 | Valid. Schedule summary had six fields. | Print the same five configurable fields installed by the cron module. |
| #55 | Valid. PAM limits do not configure service limits. | Add explicit core/process limits to cache/client units and root PAM entries. |
| #56 | Valid, including the comment about client deployment retries. | Persist restart obligations before binary/template changes, clear after verification, and detect stale cache process executables. |
| #57 | Valid under limited plays. | Exchange keys among active meta hosts in the play; document how to establish full peer trust. |
| #58 | Valid. Lookup errors were mislabeled as absent nodes. | Use ignore-not-found with name output; let API/auth/RBAC failures fail. |
| #59 | Valid. Podman used yum unconditionally. | Use the portable package module and propagate socket failures. |
| #60 | Valid. Substring/regex matching selected other mounts. | Match the complete host:port:path field against the exact local hostname/address and path. |
| #61 | Valid. Disk numbering comments contradicted defaults. | Describe the configurable offset (default 1) and offset-aware topology paths consistently. |
| #62 | Valid. Existing unmounted managed VGs became unavailable candidates. | Detect them before allocating disks; opt-in repair activates/mounts verified filesystems and repairs missing fstab entries without formatting. |
| #63 | Valid. A busy final check aborted remaining instances. | Record a deferred outcome, retain the marker, and continue other client instances. |
| #64 | Valid. An available VG name did not imply an available mount target. | Recheck VG, exact mountpoint and fstab reservations for every allocation; refuse nonempty unmounted targets. |
| #65 | Valid despite downloads being opt-in. | Require SHA256, stage privately, validate execution, atomically replace and back up the previous CLI. |
| #66 | Valid. Credential files and diffs exposed secrets. | Enforce owner-only modes, intended ownership, no_log and disabled diffs. |
| #67 | Partially addressed; credential changes are deferred by request. | Keep fixed image references. Preserve the original Grafana credential behavior; no required Vault password or password rotation. Leave the issue open. |
| #68 | Valid. Local/staged archives were trusted without verification. | Supply hashes for pinned defaults, verify staged archives, redownload corrupt online caches and reject bad local archives. |
| #69 | Valid. A failed restart entered remap rollback validation. | Separate successful-remap restarts from remap rescue; attempt every recorded service even if one start fails. |
| #70 | Valid. Check mode returned a false layout all-clear. | Run the read-only probe for real and prevent apply/stop/lock work in check mode. |
| #71 | Deferred by request to preserve existing deployment behavior. | Restore the original root service, data directory, join parameters and authentication. Do not create an account/auth file, require new credentials or migrate data. Leave the issue open. |
| #72 | Valid hardening issue. | Enable host key checking, remove the unconditional SSH override and document verified known_hosts preparation. |
| #73 | Valid. Shell interpolation and failed archives were unsafe. | Quote arguments/protected names, escape cron percent signs and remove failed temporary archives before retaining source files. |
| #74 | Valid. THP was volatile and core directories lacked sticky protection. | Persist THP using tmpfiles.d and use mode 1777 for the shared core directory. |
| #75 | Valid. Status command failures looked like empty deployments. | Distinguish explicit empty-config markers from registered stacks; propagate command errors and require recognizable status before considering deployment. |
| #77 | Partially fixed already: .gitignore covers both paths at ea44087. | Complete the remaining fix by excluding controller collections and virtualenvs from the CI YAML walker. |

New fixes should close their linked issues when merged into the default branch;
a branch or an open PR alone does not establish that the default branch is fixed.

Validation uses local Ansible fixtures and command substitutes: no production
inventory is contacted and no real disk, mount or service is modified. It covers
busy rechecks, unchanged retries with pending restarts, failed service starts,
check-mode probes, corrupt/unexecutable CLI downloads, exact mount identities,
failed archives and nonformatting disk repair.
All playbooks also receive Ansible syntax checks. Real fleet rollout and monitor
image compatibility require the site's normal deployment verification; see the
README and deployment impact review for the remaining behavior changes.

Validation results below are refreshed for the revised PR after restoring the
rqlite/Grafana credential and rqlite runtime behavior. See
[the deployment impact review](deployment-impact-review-2026-11-10.md) for the
remaining behavior changes and their triggers.

Revised local validation: all 13 focused data-path/recovery/monitor tests passed,
116 YAML files parsed and all 36 playbooks passed syntax checks. The rqlite unit,
monitor configuration template and example inventory match the pre-PR baseline
byte for byte. The revised suite contains 199 regression cases; see PR checks
for the current Linux CI result.
