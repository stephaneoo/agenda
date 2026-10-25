# Run a command against an ephemeral PostgreSQL + PostGIS on a Unix socket.
#
#   postgis-fixture <command> [args...]
#
# The command sees DATABASE_URL. The cluster lives in a temp dir and is
# destroyed when the command exits. PostgreSQL refuses to run as root, so
# when invoked as root (the dev VM) the server runs as nobody.
as_server=()
base=${TMPDIR:-/tmp}
if [ "$(id -u)" = 0 ]; then
  # nobody can't enter root's private TMPDIR (nix develop makes it 0700).
  base=/tmp
  as_server=(setpriv --reuid=65534 --regid=65534 --clear-groups)
fi
dir=$(mktemp -d "$base/agenda-pg.XXXXXX")
chmod 755 "$dir"
if [ ${#as_server[@]} -gt 0 ]; then
  chown 65534:65534 "$dir"
fi

cleanup() {
  "${as_server[@]}" pg_ctl -D "$dir/data" -m immediate stop >/dev/null 2>&1 || true
  rm -rf "$dir"
}
trap cleanup EXIT

"${as_server[@]}" initdb -D "$dir/data" -U agenda --auth=trust --encoding=UTF8 --locale=C >"$dir/initdb.log" \
  || { cat "$dir/initdb.log" >&2; exit 1; }
"${as_server[@]}" pg_ctl -D "$dir/data" -l "$dir/server.log" -w \
  -o "-k $dir -c listen_addresses='' -c fsync=off -c full_page_writes=off" start >/dev/null \
  || { cat "$dir/server.log" >&2; exit 1; }
createdb -h "$dir" -U agenda agenda
psql -h "$dir" -U agenda -d agenda -qc 'CREATE EXTENSION postgis' >/dev/null

# The socket directory goes in the host part, percent-encoded.
export DATABASE_URL="postgis://agenda@${dir//\//%2F}/agenda"
"$@"
