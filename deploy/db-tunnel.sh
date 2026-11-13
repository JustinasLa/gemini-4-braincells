#!/usr/bin/env bash
# Reach the production Postgres (Hostinger VPS) from the team server, for eval reads/writes.
#   deploy/db-tunnel.sh start   # opens an SSH tunnel to a private unix socket, writes the env file
#   deploy/db-tunnel.sh stop
#   set -a; . ~/.config/noise-radar/prod-db.env; set +a   # then: --database-url "$PROD_DATABASE_URL"
# The DB port is never published on the VPS: the tunnel goes to the db container's address.
# The socket lives in a 0700 directory and the env file is 0600, so only you can use them.
# Env: HOSTINGER_HOST (required), HOSTINGER_USER (default root), HOSTINGER_SSH_KEY (optional),
#      HOSTINGER_DIR (default /opt/noise-radar). Nothing here prints the password.
set -euo pipefail

host=${HOSTINGER_HOST:?set HOSTINGER_HOST}
user=${HOSTINGER_USER:-root}
dir=${HOSTINGER_DIR:-/opt/noise-radar}
sock_dir=${PROD_DB_SOCK_DIR:-$HOME/.cache/noise-radar/prod-pg}
env_file=${PROD_DB_ENV:-$HOME/.config/noise-radar/prod-db.env}
ctl=$sock_dir/ssh.ctl
ssh_opts=(-o BatchMode=yes -o ExitOnForwardFailure=yes)
if [[ -n ${HOSTINGER_SSH_KEY:-} ]]; then ssh_opts+=(-i "$HOSTINGER_SSH_KEY" -o IdentitiesOnly=yes); fi

case ${1:-} in
  start)
    install -d -m 700 "$sock_dir" "$(dirname "$env_file")"
    if ssh -S "$ctl" -O check "$user@$host" 2>/dev/null; then echo "tunnel already up"; exit 0; fi
    ip=$(ssh "${ssh_opts[@]}" "$user@$host" \
      "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{println}}{{end}}' noise-radar-db-1" | head -1)
    creds=$(ssh "${ssh_opts[@]}" "$user@$host" "grep -E '^POSTGRES_(USER|PASSWORD|DB)=' '$dir/.env'")
    pg_user=$(sed -n 's/^POSTGRES_USER=//p' <<<"$creds")
    pg_pass=$(sed -n 's/^POSTGRES_PASSWORD=//p' <<<"$creds")
    pg_db=$(sed -n 's/^POSTGRES_DB=//p' <<<"$creds")
    rm -f "$sock_dir/.s.PGSQL.5432"
    ssh "${ssh_opts[@]}" -f -N -M -S "$ctl" -o StreamLocalBindUnlink=yes -o StreamLocalBindMask=0177 \
      -L "$sock_dir/.s.PGSQL.5432:$ip:5432" "$user@$host"
    pass_enc=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$pg_pass")
    # single-quoted: the URL contains '&', which would background a command when sourced
    (umask 177; printf "PROD_DATABASE_URL='postgresql://%s:%s@/%s?host=%s&port=5432'\n" \
      "$pg_user" "$pass_enc" "$pg_db" "$sock_dir" >"$env_file")
    echo "tunnel up: socket $sock_dir/.s.PGSQL.5432, env in $env_file (PROD_DATABASE_URL)"
    ;;
  stop)
    ssh -S "$ctl" -O exit "$user@$host" 2>/dev/null && echo "tunnel closed" || echo "no tunnel"
    ;;
  *)
    echo "usage: $0 start|stop" >&2
    exit 2
    ;;
esac
