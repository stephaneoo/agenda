# infra: Scaleway resources (OpenTofu)

| Resource | Where | Notes |
|---|---|---|
| VM `agenda-prod`, DEV1-M x86_64 | nl-ams-1 | Debian image only for the nixos-anywhere bootstrap. 20 GB SBS root volume. |
| Block volume `agenda-zfs`, 50 GB, 5K IOPS | nl-ams-1 | The ZFS pool `tank`. |
| Flexible IPv4 | nl-ams-1 | `tofu output public_ip`. |
| Bucket `agenda-wals` | fr-par | pgBackRest repository. No versioning, private. |
| Bucket `agenda-assets` | fr-par | django-storages (empty in v1). Versioned, noncurrent versions expire after 180 days, private. |
| Bucket `agenda-tfstate` | fr-par | Tofu state, versioned. Created by hand (below), not managed here except for its policy. |
| IAM applications `pgbackrest`, `django-storage` + one API key each | | See "Scoping". |

The VM, volume, IP and buckets carry `prevent_destroy`: Tofu refuses to delete them. Deleting
any of them, or anything in the buckets, waits for Pierre.

## Running Tofu

Only from the laptop through the dev VM, which has Nix, with the Scaleway credentials from
`.env` living for that one command, and never while an untrusted-data job (travel matrix,
DATAtourisme sync) runs in the same session:

    tools/dev run-scw 'nix develop -c infra/tofu plan'
    tools/dev run-scw 'nix develop -c infra/tofu apply'

`infra/tofu` maps `SCW_ACCESS_KEY`/`SCW_SECRET_KEY` to the S3 backend's `AWS_*` variables and
`SCW_DEFAULT_PROJECT_ID` to `var.project_id`. `.terraform.lock.hcl` is committed, with hashes
for linux_amd64, darwin_arm64 and darwin_amd64. After a provider upgrade:

    tools/dev run-scw 'nix develop -c infra/tofu providers lock -platform=linux_amd64 -platform=darwin_arm64 -platform=darwin_amd64'
    tools/dev pull infra/.terraform.lock.hcl

**The state holds the API secret keys.** It lives only in `agenda-tfstate`. Never
`tofu state pull`, never copy it to the laptop or the repo (`.gitignore` refuses `*.tfstate*`,
`*.tfplan`, `.terraform/`). The keys are read once, by piping `tofu output -json` straight into
sops on the dev VM (see `docs/ops/RUNBOOK.md`, "Secrets").

`nix flake check` runs `infra-validate`: `tofu fmt -check` and `tofu validate`, offline, with
the provider from nixpkgs.

## The state bucket (one time, done 2026-10-25)

    tools/dev run-scw 'nix develop -c scw object bucket create name=agenda-tfstate region=fr-par enable-versioning=true'

Its bucket policy (admins only) is then managed by Tofu (`scaleway_object_bucket_policy.tfstate`).

## Scoping

On Scaleway an application needs an IAM policy to use Object Storage at all, and a bucket
policy then admits only the principals it names (the project owner included). So:

- each application has an IAM policy with object read/write/delete and bucket read on the
  project (`scaleway_iam_policy.object_storage`);
- every bucket in the project has a bucket policy naming the admins (`admin_principals` in
  `terraform.tfvars`) and at most one application: `agenda-wals` the `pgbackrest` app,
  `agenda-assets` the `django-storage` app, `agenda-tfstate` nobody else.

Checked on 2026-10-25: the pgbackrest key writes and lists `agenda-wals`, and gets
AccessDenied on `agenda-assets` and `agenda-tfstate`. **A new bucket in this project must get a
bucket policy too**, or both application keys can read it.

## Key expiry

The organisation gives every API key a one-year expiry. The two application keys expire on
**2027-10-25**; after that, backups fail (the MOTD shows it). Rotate before then: taint the key
(`infra/tofu apply -replace=scaleway_iam_api_key.pgbackrest`), then update the sops secret and
deploy (`docs/ops/RUNBOOK.md`). That is a backup-configuration change: it waits for Pierre.
