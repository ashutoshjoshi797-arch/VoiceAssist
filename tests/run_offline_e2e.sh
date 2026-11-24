#!/usr/bin/env bash
# Linux test harness: runs the app + Ollama + a headless browser INSIDE A NEW NETWORK NAMESPACE.
# Inside it only the loopback interface exists — no Wi-Fi/Ethernet, no route, no DNS — which is
# exactly what an air-gapped / airplane-mode computer looks like to the app.
#   unshare -rn bash tests/run_offline_e2e.sh <scenario> <wav> <expect-network> <heard> <answer> [rfkill-dir]
set -euo pipefail
cd "$(dirname "$0")/.."
SCEN=$1 WAV=$2 NET=$3 HEARD=$4 ANSWER=$5 RFKILL=${6:-}
python3 tests/netns_lo_up.py >/dev/null
echo "== network proof (inside namespace) =="
python3 - <<'PY'
import socket
for target in [("1.1.1.1", 443), ("8.8.8.8", 53)]:
    try:
        socket.create_connection(target, 2); print("UNEXPECTED: reached", target); raise SystemExit(1)
    except OSError as e: print(f"  {target[0]}: {e}")
# DNS: use a name that is not pinned in /etc/hosts (some build machines pin API hosts there)
try:
    socket.getaddrinfo("example.org", 443); print("UNEXPECTED: DNS works"); raise SystemExit(1)
except OSError as e: print("  DNS example.org:", e)
try:
    socket.create_connection(("api.anthropic.com", 443), 2); print("UNEXPECTED: reached api.anthropic.com"); raise SystemExit(1)
except OSError as e: print("  api.anthropic.com:", e)
import psutil; print("  interfaces:", list(psutil.net_if_stats()))
PY
export OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MODELS=${OLLAMA_MODELS:-/home/claude/dl/ollama-models}
${OLLAMA_BIN:-/home/claude/dl/ollama/bin/ollama} serve > /tmp/e2e-ollama.log 2>&1 &
OPID=$!
[ -n "$RFKILL" ] && export VOICEASSIST_RFKILL_DIR=$RFKILL
rm -f "$VOICEASSIST_HOME/last_url.txt" "$VOICEASSIST_HOME"/history.sqlite3* "$VOICEASSIST_HOME"/accounts.sqlite3* "$VOICEASSIST_HOME"/cloud_session.json
# accounts live in the VoiceAssist cloud: a stand-in runs on loopback so the test can sign in (as a user would have
# done once while online); the app still sees no network adapter, so it is OFFLINE / AIRPLANE and answers locally
python3 tests/fake_supabase.py 54340 > /tmp/e2e-cloud.log 2>&1 &
CPID=$!
export VOICEASSIST_SUPABASE_URL=http://127.0.0.1:54340 VOICEASSIST_SUPABASE_KEY=test-anon-key
echo "{\"local\": {\"model\": \"${LOCAL_MODEL:-qwen2.5:0.5b}\"}}" > "$VOICEASSIST_HOME/settings.json"
python3 -m voiceassist --no-window --port 8765 > /tmp/e2e-app.log 2>&1 &
APID=$!
trap 'kill $APID $OPID $CPID 2>/dev/null || true' EXIT
for i in $(seq 1 60); do [ -f "$VOICEASSIST_HOME/last_url.txt" ] && break; sleep 0.5; done
sleep 2
URL=$(cat "$VOICEASSIST_HOME/last_url.txt")
python3 tests/e2e_ui.py --url "$URL" --wav "$WAV" --scenario "$SCEN" --expect-network "$NET" \
  --expect-heard "$HEARD" --expect-answer "$ANSWER" --screenshot "/tmp/e2e-$SCEN-$NET.png"
RC=$?
echo "== app log =="; grep -E "network:|loaded|ERROR|WARN" /tmp/e2e-app.log | head -20
exit $RC
