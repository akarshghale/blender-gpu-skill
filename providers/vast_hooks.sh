# Vast.ai watchdog hooks (inserted into remote/onstart.sh by providers/vast.py).
# Vast injects CONTAINER_ID and CONTAINER_API_KEY into PID 1's environment.
# The key can only stop/start/destroy this instance.
API=${BGPU_API_BASE:-https://console.vast.ai/api/v0}
getenv() { local v="${!1}"; [ -n "$v" ] || v=$(tr '\0' '\n' </proc/1/environ 2>/dev/null | sed -n "s/^$1=//p" | head -1); echo "$v"; }
BGPU_INSTANCE_ID=$(getenv CONTAINER_ID); KEY=$(getenv CONTAINER_API_KEY)
api() { # api METHOD BODY
  if command -v curl >/dev/null; then
    curl -fsS -m 30 -X "$1" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$2" "$API/instances/$BGPU_INSTANCE_ID/"
  else
    python3 -c 'import sys,urllib.request as u; r=u.Request(sys.argv[3],data=sys.argv[2].encode(),method=sys.argv[1],headers={"Authorization":"Bearer "+sys.argv[4],"Content-Type":"application/json"}); print(u.urlopen(r,timeout=30).read().decode())' "$1" "$2" "$API/instances/$BGPU_INSTANCE_ID/" "$KEY"
  fi
}
bgpu_self_destroy() { api DELETE '{}'; }
bgpu_self_stop() { api PUT '{"state":"stopped"}'; }
