# infra: Scaleway resources (OpenTofu)

Two root modules, two states in the same bucket: `infra/` is prod (project `agenda`, state
`agenda/prod.tfstate`), `infra/dev/` the dev VM (project `agenda-dev`, state
`agenda/dev.tfstate`, below). A `tofu apply` in one never sees the other's resources.

| Resource | Where | Notes |
|---|---|---|
| VM `agenda-prod`, DEV1-M x86_64 | nl-ams-1 | Debian image only for the nixos-anywhere bootstrap. 20 GB SBS root volume. |
| Block volume `agenda-zfs`, 50 GB, 5K IOPS | nl-ams-1 | The ZFS pool `tank`. |
| Flexible IPv4 | nl-ams-1 | `tofu output public_ip`. |
| Security group `agenda-prod` | nl-ams-1 | The VM only: inbound 22, 80, 443/tcp and ICMP, the rest dropped; outbound open; stateful. |
| Bucket `agenda-wals` | fr-par | pgBackRest repository. Versioned, noncurrent versions expire after 30 days (undeleting: `docs/ops/RUNBOOK.md`, "Backups deleted from the bucket"), private. |
| Bucket `agenda-assets` | fr-par | django-storages (empty in v1). Versioned, noncurrent versions expire after 180 days, private. |
| Bucket `agenda-tfstate` | fr-par | Tofu state, versioned. Created by hand (below), not managed here except for its policy. |
| IAM applications `pgbackrest`, `django-storage` + one API key each | | See "Scoping". |

The VM, volume, IP and buckets carry `prevent_destroy`: Tofu refuses to delete them. Deleting
any of them, or anything in the buckets, waits for Pierre.

## The dev VM (`infra/dev/`)

| Resource | Notes |
|---|---|
| Project `agenda-dev` | Nothing of prod's in it. Its SSH key: `.secrets/agenda_ed25519.pub`. |
| VM `agenda-dev`, DEV1-M (3 vCPU, 4 GB), nl-ams-1 | Debian bookworm; cloud-init installs Determinate Nix 3.24.0, git, rsync, `/srv/origin-mirror.git` (`cloud-init.yaml`, first boot only). 40 GB SBS 5K root volume (the Nix store; grow it, never shrink). Powered off between sessions (`tools/dev down`). |
| Flexible IPv4 | `.secrets/dev-ip`. Flexible so that it survives power-off: a dynamic IP changes at each power-on, and the laptop's firewall (Little Snitch) admits destinations one by one. |
| Security group `agenda-dev` | Inbound 22/tcp and ICMP only. |
| IAM application `agenda-dev-ops` + policy | `InstancesServerList`, `InstancesServerRead`, `InstancesServerStart`, `InstancesServerStop` on project `agenda-dev` only. Its API key is **not** in Tofu (below). |

    tools/dev run-scw 'nix develop -c infra/dev/tofu plan'
    tools/dev run-scw 'nix develop -c infra/dev/tofu apply'

`infra/dev/tofu` takes the organisation from `SCW_DEFAULT_ORGANIZATION_ID` and unsets
`SCW_DEFAULT_PROJECT_ID` (prod's project): every resource names `agenda-dev` itself. Tofu runs
on the dev VM itself: an apply that would replace the VM is refused (`prevent_destroy`); a type
change (`server_type`) powers it off and on under Tofu's feet, so run that one from a session
that expects to lose its SSH connection, then `tools/dev up`. `state`, `image` and `user_data`
are ignored (tools/dev powers the VM; cloud-init only runs at the first boot).

**The key** (`.secrets/scw-dev.env`, 0600, on the laptop only) is created with the admin token
straight from the Scaleway API into that file, so that the secret is neither in the state nor
on the dev VM (`docs/ops/RUNBOOK.md`, "The dev VM's key"). The organisation caps API keys at one
year: this one expires on **2027-10-26**.

**What that key can do**, checked on 2026-10-26 against the live API: list and read the servers
of `agenda-dev` (its user data included: the cloud-init file, which holds no secret, and the
host key fingerprints), power them on, off, and reboot them. Refused (403): reading, starting
or stopping `agenda-prod` (prod's server list comes back empty); creating a server or an IP;
deleting or terminating a server; changing a server (tags, user data, type, the `backup`
action); reading, resizing or snapshotting a volume; creating a project or an API key; listing
IAM. It has no Object Storage permission set, and no bucket policy names it. **Worst case if
it leaks:** someone keeps the dev VM powered on (DEV1-M: €0.0202/h, ≈ €15/month at most), or
powers it off in the middle of a session or a deploy (deploy-rs rolls prod back by itself), or
reads the VM's public IP. Nothing else. Scaleway's quotas are per organisation only (no
per-project limit), so none is set; the key can't create anything for one to cap.

**A rebuild** (lost or broken VM): `tofu apply -replace=scaleway_instance_server.dev` after
lifting `prevent_destroy`, write the new server ID in `.secrets/dev-server-id` (`tofu output
server_id`; `tools/dev` reads it for every API call), then `tools/dev pin` (it checks the host
key against the fingerprints Scaleway's agent publishes in the server's user data, over the
authenticated API: no trust on first use), and a first
`tools/dev run 'nix run .#check'` (a cold store: 18 min on DEV1-M; 6 min warm). The new VM's IP is the same
flexible IP.

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
AccessDenied on `agenda-assets` and `agenda-tfstate`. Checked on 2026-10-26, with versioning
on: its delete leaves a delete marker, and deleting a version (`s3:DeleteObjectVersion`, not in
its bucket policy) gets AccessDenied. The IAM permission set `ObjectStorageObjectsDelete` can't
tell the two apart; the bucket policy does.

**Accepted for v1: Scaleway IAM can't scope a permission to one bucket**, only to a project.
So the bucket policies are the only thing keeping each key to its own bucket. **Every new
bucket in this project must get a bucket policy** (admins plus at most one application, as
`scaleway_object_bucket_policy.scoped` does), in the same change that creates it; without one,
both application keys can read, write and delete in it.

## Key expiry

The organisation gives every API key a one-year expiry. The two application keys expire on
**2027-10-25**; after that, backups fail (the MOTD shows it). Rotate before then: taint the key
(`infra/tofu apply -replace=scaleway_iam_api_key.pgbackrest`), then update the sops secret and
deploy (`docs/ops/RUNBOOK.md`). That is a backup-configuration change: it waits for Pierre.
