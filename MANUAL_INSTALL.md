# Manual install — keenetic-singbox

The exact commands `deploy.py` runs, broken out so you can step through
them yourself. For deeper context (why each step, troubleshooting, NDM
internals) see `SINGBOX_SETUP.md`.

## Prerequisites

- Entware installed in internal flash (`/opt` on UBIFS).
  Run `python kn_install_entware_step1.py` first if not.
- Dropbear SSH reachable on tcp/222.
- These four values to hand (use `monitoring/.env` to keep them out
  of shell history):

```sh
export ROUTER_HOST=192.168.X.1
export ROUTER_PASS='<router-root-password>'
export SUBSCRIPTION_URL='https://<panel>/s/<token>'
export SINGBOX_HEALTHCHECK_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

## 1. Generate config locally

From `keenetic-singbox/` on your workstation:

```sh
python sub_to_singbox.py "$SUBSCRIPTION_URL" \
       --out hynet_singbox.json \
       --ndm-setup ndm_setup.cmd \
       --router-ip "$ROUTER_HOST"
/opt/bin/sing-box check -c hynet_singbox.json   # optional pre-flight if you have sing-box locally
```

## 2. Install Entware packages on the router

```sh
ssh -p 222 root@"$ROUTER_HOST" '
  opkg update &&
  opkg install sing-box-go python3 python3-urllib python3-codecs cron curl &&
  /opt/etc/init.d/S10cron start 2>/dev/null;
  mkdir -p /opt/etc/sing-box /opt/var/lib/sing-box /opt/share/sing-box/ui \
           /opt/etc/cron.1min /opt/etc/cron.daily
'
```

(`opkg install` is idempotent — re-running for upgrades is safe.)

## 3. Push files to the router

Dropbear has no SFTP, so base64-pipe over the SSH exec channel. From
`keenetic-singbox/`:

```sh
SSH="ssh -p 222 root@$ROUTER_HOST"

push() {
    local local_file=$1 remote=$2 mode=${3:-0644}
    base64 < "$local_file" | $SSH "base64 -d > $remote && chmod $mode $remote"
}

push hynet_singbox.json              /opt/etc/sing-box/config.json
push S99singbox-healthcheck          /opt/etc/init.d/S99singbox-healthcheck         0755
push singbox-healthcheck-watchdog    /opt/etc/cron.1min/singbox-healthcheck-watchdog 0755
push sub-refresh.sh                  /opt/etc/cron.daily/sub-refresh                0755
push sub_to_singbox.py               /opt/share/sing-box/sub_to_singbox.py
```

Then write the subscription URL (separate so the token isn't echoed in
your shell history file):

```sh
printf '%s' "$SUBSCRIPTION_URL" | \
    $SSH 'cat > /opt/etc/sing-box/.subscription-url && chmod 600 /opt/etc/sing-box/.subscription-url'
```

## 4. Validate config on the router

```sh
$SSH '/opt/bin/sing-box check -C /opt/etc/sing-box/'
```

If this fails, fix `hynet_singbox.json` locally and re-push. Don't move on.

## 5. Apply NDM-side OpkgTun0 registration

NDM owns the kernel iface name, so sing-box must be stopped first or
NDM will refuse with `0xcffd009f`. Apply each non-comment line of
`ndm_setup.cmd` via `ndmc`:

```sh
$SSH '/opt/etc/init.d/S99sing-box stop 2>/dev/null; sleep 2'

while IFS= read -r line; do
    case "$line" in \!*|"") continue ;; esac
    $SSH "ndmc -c '$line'"
done < ndm_setup.cmd

$SSH "ndmc -c 'system configuration save'"
```

## 6. Start sing-box + healthcheck daemon

```sh
$SSH '
  /opt/etc/init.d/S99sing-box start &&
  sleep 6 &&
  /opt/etc/init.d/S99singbox-healthcheck start
'
```

## 7. Verify

```sh
$SSH '
  echo "--- sing-box process ---"; pgrep -af sing-box | head -3
  echo "--- opkgtun0 ---";          ip a show opkgtun0 | head -5
  echo "--- Clash API ---";         netstat -tln | grep 9090
  echo "--- healthcheck status ---"; /opt/etc/init.d/S99singbox-healthcheck status | head -10
'
```

Expected:
- one `sing-box run …` process,
- `opkgtun0` UP with `172.19.0.1/32`,
- `0.0.0.0:9090` LISTEN.

Open MetaCubeXD: `http://$ROUTER_HOST:9090/ui/`,
secret = `$SINGBOX_HEALTHCHECK_SECRET`.

## 8. Pin a service to the tunnel (optional)

Example for YouTube — repeat the pattern for any FQDN group:

```sh
$SSH "
  ndmc -c 'object-group fqdn youtube'
  ndmc -c 'object-group fqdn youtube include youtube.com'
  ndmc -c 'object-group fqdn youtube include www.youtube.com'
  ndmc -c 'object-group fqdn youtube include googlevideo.com'
  ndmc -c 'dns-proxy route object-group youtube OpkgTun0 auto reject'
  ndmc -c 'system configuration save'
"
```

The trailing `reject` is the kill-switch — if `OpkgTun0` is down, traffic
to those FQDNs gets blackholed instead of leaking through PPPoE.

## Re-running

- **Subscription rotation only** (provider rotated tokens / nodes):
  the daily cron `/opt/etc/cron.daily/sub-refresh` handles it. Force a
  refresh now: `$SSH /opt/etc/cron.daily/sub-refresh`.
- **Code-side change** (you edited `S99singbox-healthcheck` etc.):
  re-run step 3 for that file, then restart the affected service.
- **Router IP change** (rare): regenerate config (step 1 with new
  `--router-ip`), re-push (step 3), re-apply ndm_setup (step 5).
