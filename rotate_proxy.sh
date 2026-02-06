#!/bin/bash
# Rotate the proxy VM's IP by stop/start and re-establish SSH SOCKS tunnel.
# Usage: ./rotate_proxy.sh

VM_NAME="yad2-proxy3"
ZONE="europe-west1-b"
SOCKS_PORT=1080

echo "[$(date)] Rotating proxy IP..."

# 1. Kill existing SSH tunnel
fuser -k ${SOCKS_PORT}/tcp 2>/dev/null
sleep 1

# 2. Stop VM
echo "[$(date)] Stopping VM..."
gcloud compute instances stop $VM_NAME --zone=$ZONE --quiet 2>&1
if [ $? -ne 0 ]; then
    echo "[$(date)] ERROR: Failed to stop VM"
    exit 1
fi

# 3. Start VM (gets new ephemeral IP)
echo "[$(date)] Starting VM..."
gcloud compute instances start $VM_NAME --zone=$ZONE --quiet 2>&1
if [ $? -ne 0 ]; then
    echo "[$(date)] ERROR: Failed to start VM"
    exit 1
fi

# 4. Get new IP
NEW_IP=$(gcloud compute instances describe $VM_NAME --zone=$ZONE --format='get(networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null)
echo "[$(date)] New IP: $NEW_IP"

# 5. Wait for SSH to be ready
echo "[$(date)] Waiting for SSH..."
for i in $(seq 1 30); do
    gcloud compute ssh $VM_NAME --zone=$ZONE --quiet --command="echo ok" 2>/dev/null && break
    sleep 2
done

# 6. Create SOCKS tunnel
echo "[$(date)] Creating SOCKS tunnel on port $SOCKS_PORT..."
gcloud compute ssh $VM_NAME --zone=$ZONE --quiet --ssh-flag="-D $SOCKS_PORT -f -N -4" 2>&1

# 7. Verify
sleep 2
PROXY_IP=$(curl -s --max-time 5 --socks5-hostname 127.0.0.1:$SOCKS_PORT https://httpbin.org/ip 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['origin'])" 2>/dev/null)

if [ -n "$PROXY_IP" ]; then
    echo "[$(date)] SUCCESS: Proxy active at $PROXY_IP"
    exit 0
else
    echo "[$(date)] WARNING: Tunnel created but verification failed"
    exit 1
fi
