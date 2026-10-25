# Run a command against an ephemeral PostgreSQL + PostGIS on a Unix socket.
#
#   postgis-fixture <command> [args...]
#
# The command sees DATABASE_URL. The cluster lives in a temp dir and is
# destroyed when the command exits. PostgreSQL refuses to run as root, so
# when invoked as root (the dev VM) the server runs as nobody.
#
# Layout: the top dir and the logs belong to the caller. Only data/ and sock/ are
# handed to the server user, and the socket is 0700, so only that user (and root)
# can connect to the trust-auth cluster.
as_server=()
base=${TMPDIR:-/tmp}
if [ "$(id -u)" = 0 ]; then
  # nobody can't enter root's private TMPDIR (nix develop makes it 0700).
  base=/tmp
  as_server=(setpriv --reuid=65534 --regid=65534 --clear-groups)
fi
dir=$(mktemp -d "$base/agenda-pg.XXXXXX")
chmod 755 "$dir"
mkdir -m 700 "$dir/data" "$dir/sock"
if [ ${#as_server[@]} -gt 0 ]; then
  chown 65534:65534 "$dir/data" "$dir/sock"
fi

# The server tools get a clean environment: nothing of the caller's leaks into them.
server() {
  "${as_server[@]}" env -i PATH="$PATH" LANG=C "$@"
}

cleanup() {
  server pg_ctl -D "$dir/data" -m immediate stop >/dev/null 2>&1 || true
  rm -rf "$dir"
}
trap cleanup EXIT

# The logs are opened by this shell, in a dir the server user can't write to.
server initdb -D "$dir/data" -U agenda --auth=trust --encoding=UTF8 --locale=C >"$dir/initdb.log" 2>&1 \
  || { cat "$dir/initdb.log" >&2; exit 1; }
server pg_ctl -D "$dir/data" -w \
  -o "-k $dir/sock -c listen_addresses='' -c unix_socket_permissions=0700 -c fsync=off -c full_page_writes=off" \
  start >"$dir/server.log" 2>&1 \
  || { cat "$dir/server.log" >&2; exit 1; }
createdb -h "$dir/sock" -U agenda agenda
psql -h "$dir/sock" -U agenda -d agenda -qc 'CREATE EXTENSION postgis' >/dev/null

# The socket directory goes in the host part, percent-encoded.
export DATABASE_URL="postgis://agenda@${dir//\//%2F}%2Fsock/agenda"
"$@"
