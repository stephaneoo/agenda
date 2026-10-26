# Runbook: operating the agenda

Written for **Claude**, working with the colleague, who is not technical and is not expected
to judge commands. Claude runs the commands, explains in plain French what happened, and
stops at anything listed under "Waits for Pierre". Every action below gives the command,
what success looks like, and what to do when it fails.

Rule of thumb: everything here is undoable except what deletes data. Deleting data (backups,
datasets, buckets, rows) is never a safe action.

## The system in one page

| | |
|---|---|
| Site | `https://51.15.79.107/` (no domain yet; a Let's Encrypt certificate for the IP itself) |
| Server | `agenda-prod`, Scaleway DEV1-M (3 vCPU, 4 GB), nl-ams-1, NixOS. SSH: `root@51.15.79.107` |
| Database | PostgreSQL 16 + PostGIS, data on ZFS `tank/pg` (block volume, 50 GB), hourly snapshots kept 48 h |
| Backups | pgBackRest to the bucket `agenda-wals` in fr-par (another region), encrypted. RPO 5 min, RTO 4 h |
| Config | everything in this repo: `nix/hosts/agenda/`, deployed by `nix run .#deploy` |
| Infra | `infra/` (OpenTofu), see `infra/README.md` |

Services on the host: `caddy` (TLS, static files, rate limits, proxies to gunicorn),
`agenda-web.socket` + `agenda-web` (gunicorn), `agenda-migrate` (migrations and geo data, at
each deploy), `postgresql`, `agenda-egress` (outbound allow-list refresh).

Timers (Europe/Paris), each records its outcome in `/var/lib/agenda/status.json`:

| Timer | When | STALE after |
|---|---|---|
| `sync-datatourisme` | daily 06:00 | 26 h |
| `pgbackrest-backup` | daily 01:30 | 26 h |
| `pgbackrest-tier-expire` | daily 02:30 | 26 h |
| `restore-test` | the 2nd of each month, 04:00 | 32 days |
| `zfs-snapshot-hourly` | hourly | (not in the status file) |

## First time on the Mac

The colleague deploys from a Mac. Once, with Claude:

1. **Install Nix**: the Determinate Systems installer (flakes on by default):
   `curl -fsSL https://install.determinate.systems/nix | sh -s -- install`, then open a new
   terminal. Success: `nix --version` prints a version.
2. **Get the repo**: `git clone <the repo URL> agenda && cd agenda`. Git needs to be able to
   push (the deploy pushes a tag): check with `git push --dry-run origin main`.
3. **An SSH key for the server**: `ssh-keygen -t ed25519 -f ~/.ssh/agenda -C "<name>@mac"`
   (a passphrase is fine; macOS keeps it in the keychain). Send **the `.pub` file only** to
   Pierre. He adds it to `rootKeys` in `nix/hosts/agenda/site.nix` and deploys; until then the
   server refuses the key.
4. **Tell the commands which key**: add `export AGENDA_SSH_KEY=~/.ssh/agenda` to `~/.zshrc`,
   open a new terminal.
5. **Check**: `nix run .#status`. Success: the status report. The first run downloads a few
   hundred MB.
6. **A first `nix run .#check`**: it builds the test environment for the Mac (long the first
   time) and runs every check. Success: it ends without an error.

The server is the only Linux machine: the system is built **on the server** during a deploy
(`nix build --store ssh-ng://…`), and the browser test (e2e) runs there too. Nothing needs a
Linux VM on the Mac.

**Not verified on a Mac yet** (nothing was ever built on darwin in the build session; the
apps only evaluate): `nix run .#check` (pytest with PostGIS, the JS tests, the infra check on
darwin), `nix run .#deploy` end to end (the flake copy for the e2e, the remote build, deploy-rs
from darwin), the admin tunnel in Safari (Secure cookies on `http://localhost`). Do these
with Pierre the first time, and fix what breaks before relying on them.

## Status

    nix run .#status

The same report as at every SSH login (the MOTD). Each job shows `ok`, `PARTIAL`, `FAILED`
or `STALE` (no success within its interval), then disk and ZFS pool usage. The last line says
`all jobs ok` or lists what needs attention.

- `sync-datatourisme` FAILED or PARTIAL: see "The sync" below.
- `pgbackrest-backup` FAILED or STALE: read the log (`journalctl -u pgbackrest-backup`); a
  network error: run it again (`systemctl start pgbackrest-backup`). Anything else, or two
  days in a row: Pierre.
- `restore-test` FAILED: Pierre (the backups may not be restorable). Do not change anything.
- disk or pool over 80%: Pierre.

Every Claude session starts by offering this report (see `CLAUDE.md`).

## Safe actions

### Deploy

    nix run .#deploy

Success: it ends with `Deployed <commit> as deploy-<timestamp>.` and the tag is on origin.
It refuses (and changes nothing) when:
- the tree has uncommitted changes, or `main` isn't `origin/main`: commit and push (or pull)
  first;
- a check fails: read the failing test, fix it in the code, commit, deploy again. A failed
  migration lint means a schema change must be split into expand and contract (`CLAUDE.md`).

If the new system doesn't confirm, deploy-rs puts the previous one back by itself ("magic
rollback"). If `/healthz` fails after activation, the deploy switches back to the previous
generation and says so. Either way the site keeps running on the old version; read the
output, fix, deploy again.

If the tag push failed at the end: `git push origin deploy-<timestamp>` (the next deploy's
migration check compares against it).

### Rollback (to the previous system generation)

    ssh root@51.15.79.107 'nix-env -p /nix/var/nix/profiles/system --rollback && /nix/var/nix/profiles/system/bin/switch-to-configuration switch'

(Not `nixos-rebuild --rollback`: it wants a NixOS configuration on the server, and there is
none; the configuration lives in this repo.) Success: it prints the units it stopped and started, then
`ssh root@51.15.79.107 "curl -sS -H 'Host: 127.0.0.1' http://127.0.0.1:8000/healthz"` answers
`{"status": "ok", …, "debug": false}` and `nix run .#status` works. Each run goes one
generation further back. If it fails, tell Pierre.

To go forward again to the newest generation (after the problem is understood), or simply
deploy `main` again with `nix run .#deploy`:

    ssh root@51.15.79.107 'nix-env -p /nix/var/nix/profiles/system --list-generations'
    ssh root@51.15.79.107 'nix-env -p /nix/var/nix/profiles/system --switch-generation <N> && /nix/var/nix/profiles/system/bin/switch-to-configuration switch'

A rollback does not undo migrations (they are expand-only between two deploys, so the
previous code runs on the new schema). It does not touch data.

### Re-run the sync

    ssh root@51.15.79.107 'systemctl start sync-datatourisme && journalctl -u sync-datatourisme -n 5 -o cat'

Success: a line `clean: … rows, … added, …` and `nix run .#status` shows the sync `ok`. It
takes about 3 minutes.

### Read the logs

    ssh root@51.15.79.107 'journalctl -u <unit> -n 100 --no-pager'

Units: `agenda-web` (the site), `sync-datatourisme`, `pgbackrest-backup`,
`pgbackrest-tier-expire`, `restore-test`, `agenda-migrate`, `caddy`, `postgresql`,
`agenda-egress`. Caddy's access log: `/var/log/caddy/`.

### Restore

Always a test first:

    nix run .#restore -- --test-only

Success: `test restore of the latest backup: sanity checks passed in N s`. It restores the
latest backup into the scratch dataset `tank/pg-restore` with a temporary PostgreSQL, checks
it (migrations match the code, tables non-empty, latest claim no newer than the target), then
drops the scratch copy. The live database is never touched. (2026-10-26 drill: see "Drills".)

The real restore, when data was lost or damaged, **with the colleague's agreement on the
moment to restore to**:

    nix run .#restore -- --target-time 2026-11-02T21:00:00+01:00   # any time in the last 7 days
    nix run .#restore -- --set 20261101-013002F                    # a backup set (pgbackrest info)
    nix run .#restore --                                           # the latest state

It does the test steps, then snapshots the live dataset, stops the site, swaps the datasets,
starts it and checks `/healthz`. The site is down for a minute or two. If anything fails
after the swap, it swaps back by itself and says so. Success: `restored. The previous dataset
is kept (tank/pg-prev-…)`. Then it takes a full backup. **The previous dataset stays; deleting
it (after 7 days) waits for Pierre.** List backups: `ssh root@51.15.79.107 'runuser -u postgres -- pgbackrest --stanza=agenda info'`.

For a mistake made in the last 48 h, the ZFS snapshots are faster, but rolling the pool back
is a ZFS change: Pierre.

### Open the admin (moderation)

The admin is not on the public site (`/admin/` is a 404 there). Through an SSH tunnel:

    nix run .#admin-tunnel

then open `http://localhost:8000/admin/` in the browser; Ctrl-C in the terminal closes it.
(Equivalent: `ssh -N -L 8000:127.0.0.1:8000 root@51.15.79.107`.) Logins: Django accounts;
`pierre` is the superuser (password in Pierre's `.secrets/admin-pierre.txt`). New moderator:
in the admin, Users, add, with "Staff status" and the group "Modérateurs".

### Unlock a moderator

After 5 failed logins an account is locked for an hour.

    ssh root@51.15.79.107 'agenda-manage axes_reset_username <name>'

Success: `1 attempts removed` (or similar). Then they log in again.

### The sync

The summary line (status report, or `ingest.SyncRun` in the admin) says what happened.

- **`partial`, "N withdrawals skipped"**: the run would have withdrawn more than 20% of the
  area's future occurrences, so it withdrew nothing. Check upstream (the DATAtourisme data on
  data.gouv.fr: did those events really go?). Only if the withdrawals are right:

      ssh root@51.15.79.107 'agenda-manage sync_datatourisme --allow-mass-withdrawal=N'

  with that same N. The command refuses any other count, and applies them only if it would
  withdraw exactly N again with rows fetched; otherwise it stays partial and its warning says
  why.
- **`partial`, rows refused**: some rows didn't pass validation; the rest applied. Read the
  journal for the keys.
- **"refused for too many dates"**: a row expanding to over 400 occurrences in the window
  (run date − 1 year to + 2 years). Look at the row upstream; its dates stay as they were.
- **`failed`, "past its deadline"**: upstream too slow (15 minutes,
  `AGENDA_SYNC_DEADLINE_SECONDS`). Run it again; nothing was withdrawn.
- **`failed`, a fetch error**: upstream or network. Nothing was withdrawn. Run it again later.
- A **clean** run whose summary says "withdrew N%" with a large N: a drift to look at with Pierre.
- `unit stopped: timeout` or `oom-kill`: the run hit its 1 h or 512 MB bound. Pierre.

When parse or mapping code changes what a row becomes, bump `ingest.sync.PARSER_VERSION`.

### Moderation edge cases

- A moved occurrence's new date belongs to moderation. If the event later disappears
  upstream, the moved one stays shown: hide it by hand in the admin.
- After any change to a `Source` (priority, label, licence, external), the admin reprojects
  everything. By hand: `ssh root@51.15.79.107 'agenda-manage rebuild_projection'`.

### Service worker kill switch

If offline copies must go (a bad worker, hidden pages leaked into caches): in
`nix/hosts/agenda/app.nix`, add `AGENDA_SW_KILL = "1";` to `env`, commit, deploy. Each
browser clears the site's caches and unregisters the worker on its next visit (the IndexedDB
copy stays, but without the worker no page loads offline). To bring offline reading back:
remove it, commit, deploy.

### The status file doesn't update

With "status file not written" in a job's journal: `/var/lib/agenda` is group- or
world-writable, or not owned by `agenda`. Fix the directory, never loosen the check:

    ssh root@51.15.79.107 'chown agenda:agenda /var/lib/agenda && chmod 0750 /var/lib/agenda'

### The certificate

Let's Encrypt certificates for an IP address last about 6 days; Caddy renews them by itself.
If browsers report a certificate error: `journalctl -u caddy | grep -i -E "certif|acme"`. A
renewal failure that lasts more than a day: Pierre.

## Waits for Pierre

Never do these, even if asked; explain that Pierre decides, and leave him a note:

- changing the backup configuration or retention (`nix/hosts/agenda/backup.nix`,
  `tools/backup_tiers.py`), including rotating the S3 keys;
- deleting backups, buckets, volumes or ZFS datasets (`tank/pg-prev-*` included, after a
  restore), or any other change to the ZFS pool (rollback to a snapshot, properties);
- manual SQL (`psql`, `dbshell`, raw queries) on the database;
- `infra/` changes (`tofu apply`), the VM size, anything billed;
- changing secrets (`secrets/agenda.yaml`) or who may SSH in (`rootKeys`).

## Secrets

| Secret | Where |
|---|---|
| Django secret key, pgBackRest S3 key + secret, repository passphrase, django-storage S3 key + secret | `secrets/agenda.yaml`, sops-encrypted to the host's SSH key and the operator age key (`.sops.yaml`); only the encrypted file is committed |
| Operator age key | `.secrets/age-operator.txt` on Pierre's laptop (never committed) |
| Repository passphrase, a copy | `.secrets/pgbackrest-cipher-pass.txt` on Pierre's laptop. **Without it the backups can't be read**: keep a copy outside the laptop (password manager) |
| `pierre` admin password | `.secrets/admin-pierre.txt` on Pierre's laptop |
| SSH key for root | `.secrets/agenda_ed25519` (Claude's); each person's own key otherwise |
| Tofu state (holds the API keys) | only in the bucket `agenda-tfstate` |

Editing a secret (Pierre): `SOPS_AGE_KEY_FILE=.secrets/age-operator.txt sops secrets/agenda.yaml`,
commit, deploy. The S3 keys came from `infra/tofu output -json`, piped straight into sops on
the dev VM; they expire on **2027-10-25** (`infra/README.md`, Key expiry).

## Drills

- 2026-10-26, restore test (`nix run .#restore -- --test-only`) of the first backup: sanity
  checks passed in **32 s** (45 MB database: restore from fr-par 25 s, WAL replay 5 s,
  checks 2 s). The monthly `restore-test` unit passed the same day.
- 2026-10-26, rollback and forward again, following "Rollback" above: generation 7 to 6 and
  back to 7, `/healthz` ok (`"debug": false`) at each step.
- 2026-10-26, first `nix run .#deploy` (run from the dev VM, standing in for the laptop):
  every check, the build on the host, deploy-rs, `/healthz`, tag `deploy-20261026T094007Z`.

## Rebuilding the server (Pierre)

The first install, for the record (2026-10-26), and what a rebuild after losing the VM would
repeat: the VM from `infra/` (Tofu); NixOS with nixos-anywhere from a Linux machine with Nix,
`nix run github:nix-community/nixos-anywhere -- --flake .#agenda --extra-files <dir> --target-host root@<ip>`,
where `<dir>/etc/ssh/ssh_host_ed25519_key` is the host key whose age recipient is in
`.sops.yaml` (a new key means `sops updatekeys` first); then `migrate` and `load_geo` run by
themselves (`agenda-migrate`), the static files are built with the system, and by hand: a
first `systemctl start sync-datatourisme`, the superuser
(`agenda-manage createsuperuser`), a first `systemctl start pgbackrest-backup`, and a restore
test. With the ZFS volume intact, the database comes back as it was; without it, restore it
(`nix run .#restore`).
