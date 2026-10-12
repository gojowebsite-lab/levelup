#!/data/data/com.termux/files/usr/bin/bash
# AZIM-MATCH - 4 account guild glory bot
cd "$(dirname "$0")" || exit 1
echo "[AZIM-MATCH] logging to match.log (Ctrl+C stops the bot)"

# Ctrl+C must stop the whole pipeline, not just tee.  Without this trap the
# background python child could survive as an orphan and a later run would
# create duplicate account sessions.
cleanup() {
    trap - INT TERM EXIT
    jobs -pr | xargs -r kill 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT
# Keep the diagnostic log bounded.  Preserve the newest lines, but never let
# the file grow to 1 MiB or more.
MAX_LOG_BYTES=1048576
KEEP_LOG_BYTES=900000
if [ -f match.log ] && [ "$(wc -c < match.log)" -ge "$MAX_LOG_BYTES" ]; then
    tail -c "$KEEP_LOG_BYTES" match.log > match.log.tmp && mv match.log.tmp match.log
fi
# -u = unbuffered, otherwise nothing appears until the pipe buffer fills up
python3 -u Main.py 2>&1 | tee -a match.log &
PIPE_PID=$!

# Trim periodically during long-running sessions as well as at startup.
while kill -0 "$PIPE_PID" 2>/dev/null; do
    if [ -f match.log ] && [ "$(wc -c < match.log)" -ge "$MAX_LOG_BYTES" ]; then
        tail -c "$KEEP_LOG_BYTES" match.log > match.log.tmp && mv match.log.tmp match.log
    fi
    sleep 5
done
wait "$PIPE_PID"
