#!/bin/bash
# Keep testing whether Lengau's login node is reachable from this Mac, and
# tell me the moment it is.
#
#   tools/wait_for_lengau.sh              # check every 5 min
#   tools/wait_for_lengau.sh 2            # check every 2 min
#   NTFY_TOPIC=my-secret-topic tools/wait_for_lengau.sh   # also push to phone
#
# "Reachable" means what ssh needs: the name resolves and the SSH server on
# port 22 answers the handshake. It never logs in, so repeated checks cannot
# trip the login node's failed-login lockout. Connect the VPN whenever you
# like - the next check picks it up. Ctrl-C to give up.

HOST=${LENGAU_HOST:-lengau.chpc.ac.za}
PORT=22
INTERVAL_MIN=${1:-5}

notify() {
  osascript -e "display notification \"$2\" with title \"$1\" sound name \"Glass\"" 2>/dev/null
  if [[ -n "$NTFY_TOPIC" ]]; then
    curl -s -m 10 -H "Title: $1" -d "$2" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null
  fi
}

# Don't let the Mac sleep through the wait.
caffeinate -i -w $$ &

echo "Waiting for $HOST:$PORT, checking every $INTERVAL_MIN min (Ctrl-C to stop)"
tries=0
while true; do
  tries=$((tries + 1))
  now=$(date +%H:%M)

  # Resolve with the system resolver, the same one ssh uses (it honours the
  # VPN's DNS and the local cache, unlike dig).
  ip=$(dscacheutil -q host -a name "$HOST" | awk '/^ip_address/ {print $2; exit}')
  if [[ -z "$ip" ]]; then
    echo "$now  #$tries  name does not resolve (DNS failure)"
  else
    # ssh-keyscan completes the SSH handshake up to the host keys and stops
    # there - no authentication is attempted.
    if ssh-keyscan -T 10 -p "$PORT" "$HOST" 2>/dev/null | grep -q .; then
      echo "$now  #$tries  UP - $HOST ($ip) is answering ssh"
      notify "Lengau is reachable" "$HOST answered at $now - you can log in."
      exit 0
    fi
    echo "$now  #$tries  resolves to $ip but ssh is not answering"
  fi
  sleep $((INTERVAL_MIN * 60))
done
