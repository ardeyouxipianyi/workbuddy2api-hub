# WorkBuddy Multi-Account Reverse Proxy Gateway
FROM python:3.11-alpine

# Set environment
ENV PYTHONUNBUFFERED=1     HOST=0.0.0.0     PORT=8788     API_KEY=     TZ=Asia/Shanghai

WORKDIR /app

# Alpine timezone & certs
RUN apk add --no-cache tzdata ca-certificates &&     cp /usr/share/zoneinfo/${TZ} /etc/localtime &&     echo "${TZ}" > /etc/timezone

# Copy application files (Zero external pip dependencies needed)
COPY wb_*.py dashboard.html ./

# Create data directories
RUN mkdir -p /app/accounts /app/usage

# Volume persistence for credentials and usage logs
VOLUME ["/app/accounts", "/app/usage"]

EXPOSE 8788

# Health probe for docker / orchestrators: /health always answers 200 and
# only exposes account identity to an authorised caller. The URL follows
# PORT so a remapped container still reports healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8788'), timeout=3)" || exit 1

# Launch the proxy in the project's LAN mode: --lan listens on every interface
# and forces an api key (generated once, persisted in ./accounts/settings.json
# and printed in the startup log). Without it the container published port 8788
# to the network while key checking stayed off.
# No --port on purpose: the gateway already reads PORT from the environment
# (default 8788), and the exec form cannot expand a variable. Keeping the exec
# form leaves python as PID 1, so `docker stop` still delivers SIGTERM.
CMD ["python", "wb_proxy.py", "--host", "0.0.0.0", "--lan"]
