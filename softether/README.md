# SoftEther via NDM Bridge2 — install + operate

Alternative routing mode for the same Keenetic stack: instead of (or in
addition to) sing-box's `OpkgTun0`, route selected FQDN groups through a
SoftEther HUB via NDM's `Bridge2`. Useful when the egress you want is a
SoftEther server (existing infra, specific geography), not one of the
sing-box outbounds.

The two modes coexist on one router — sing-box owns `OpkgTun0`, this
mode owns `Bridge2`, and NDM's `dns-proxy route object-group <X>
{OpkgTun0|Bridge2} auto reject` decides which FQDN goes where.

## TL;DR — interactive installer

```sh
ssh -p 222 root@<router-ip>
curl -fsSL https://raw.githubusercontent.com/inlarin/keenetic-singbox/main/install-softether.sh | sh
```

`install-softether.sh` prompts for server / port / HUB / username /
password / profile, then does the rest automatically (opkg install,
`vpncmd` setup, NDM Bridge2, DHCP-based HUB gateway capture, conf-file
write, watcher deployment).

Read on for the manual procedure — useful for debugging or when you
want to step through the install.

> Note: all examples below use placeholders like `<router-lan-ip>`,
> `<vpn-username>`, `vpn.example.com`, and `<profile>`. Replace with
> your own values.

---

## Architecture (one diagram)

```
LAN client (<router-lan-subnet>.X)
  │
  │ DNS to <router-lan-ip> → NDM dnsmasq resolves FQDN from object-group
  │ NDM populates ipset _NDM_OGDN_4_@<group>
  │
  │ HTTP/etc. packet to public IP
  ▼
Router (NDM)
  │ iptables -t mangle PREROUTING: ipset match → MARK 0xffffXXX
  │ ip rule:    fwmark 0xffffXXX → table N
  │ table N:    default via <hub-gateway> dev br2     ← patched by S05vpnclient
  ▼
br2 (NDM Bridge2, Linux bridge)
  │ iptables -t nat POSTROUTING -o br2 → SNAT to <hub-leased-ip>
  ▼
vpn_<profile> (SoftEther TAP, member of br2)
  │ vpnclient (Entware) → SSL/TLS to TCP/443
  ▼
vpn.example.com  (HUB="<HUB-name>", user "<vpn-username>")
  │
  ▼
internet (HUB exit IP)
```

---

## Prerequisites (one-time, NDM web UI)

In the router's web UI → **System settings → Component options**, enable
and apply (router will reboot):

- **OPKG** — package manager
- **Ext file system**
- **OpenVPN client** — on this firmware family, NDM does not create
  kernel-side TAP/TUN devices correctly without it, and our setup won't
  work
- *(optional)* DNS-over-TLS / DNS-over-HTTPS proxy — independent

After reboot, verify via NDM telnet (`telnet admin@<router-lan-ip>`):

```
show version
# components: ...,openvpn,opkg,...   ← both must be present
```

---

## Step 1 — Entware in NAND

Via NDM CLI (telnet — Entware/SSH isn't up yet):

```
opkg disk storage:/ https://bin.entware.net/aarch64-k3.10/installer/aarch64-installer.tar.gz
system configuration save
system reboot
```

After reboot dropbear listens on tcp/222 (factory default password from
Keenetic docs):

```
ssh -p 222 root@<router-lan-ip> 'opkg update'
```

---

## Step 2 — SoftEther client + base deps

```sh
ssh -p 222 root@<router-lan-ip>     # change the default password on first login: passwd

opkg install softethervpn5-libs softethervpn5-client \
             iptables ipset ip-full
```

About 50 MB. After install `ls /opt/etc/init.d/` already contains
`S05vpnclient` (placed by the package — we'll replace it with our
NDM-aware variant in step 5).

---

## Step 3 — SoftEther account via `vpncmd`

> Quirks: `vpncmd /CMD ...` takes arguments as separate argv tokens.
> Don't quote the whole command — quotes end up in the command name and
> vpncmd reports `Command not found`. In Git-Bash on Windows, prefix
> the call with `MSYS_NO_PATHCONV=1` so paths like `/CMD` aren't
> rewritten to Windows form.

```sh
# 1. Create a virtual TAP named <profile> → kernel iface vpn_<profile>
vpncmd /CLIENT localhost /CMD NicCreate <profile>

# 2. Create the VPN connection setting
vpncmd /CLIENT localhost /CMD AccountCreate <profile> \
   /SERVER:vpn.example.com:443 \
   /HUB:<HUB-name> \
   /USERNAME:<vpn-username> \
   /NICNAME:<profile>

# 3. Set the password (standard password authentication)
vpncmd /CLIENT localhost /CMD AccountPasswordSet <profile> \
   /PASSWORD:<your_password> /TYPE:standard

# 4. Enable autoconnect on daemon start (important — otherwise after
#    reboot vpnclient will be running but disconnected)
vpncmd /CLIENT localhost /CMD AccountStartupSet <profile>

# 5. Connect now
vpncmd /CLIENT localhost /CMD AccountConnect <profile>
sleep 5
vpncmd /CLIENT localhost /CMD AccountStatusGet <profile>
# Session Status should be "Connection Completed (Session Established)"
```

Linux now has `vpn_<profile>` TAP with `<UP,LOWER_UP>`, but **without
IPv4** — DHCP is handled by our watcher (step 5).

---

## Step 4 — NDM Bridge2 (telnet)

```
ndmc -c "interface Bridge2"
ndmc -c "interface Bridge2 description \"SoftEther vpn_<profile> via L2 bridge\""
ndmc -c "interface Bridge2 role misc"
ndmc -c "interface Bridge2 security-level public"
ndmc -c "interface Bridge2 ip mtu 1500"
ndmc -c "interface Bridge2 ip tcp adjust-mss pmtu"
ndmc -c "interface Bridge2 ip global auto"
ndmc -c "interface Bridge2 up"
system configuration save
```

> NDM creates the kernel-side bridge `br2`. You **cannot** add
> `vpn_<profile>` to it via `ndmc include` — the TAP is not an
> NDM-known iface. The watcher does that via `brctl`.

---

## Step 5 — Watcher: S05vpnclient + udhcpc.br2.script + conf

First, write the local conf with your specific values:

```sh
ssh -p 222 root@<router-lan-ip> 'cat > /opt/etc/softether-bridge.conf <<EOF
VPN_IFACE=vpn_<profile>
HUB_GW=<hub-gateway>
EOF
chmod 600 /opt/etc/softether-bridge.conf'
```

Where:
- `VPN_IFACE` — the kernel TAP name from `vpncmd NicCreate <profile>`,
  i.e. `vpn_<profile>`.
- `HUB_GW` — the gateway your HUB hands out via DHCP (typically a
  private subnet's `.1`). Run a quick `udhcpc -i br2 -s
  /opt/etc/udhcpc.br2.script` to find out what your HUB serves; the
  log line will contain `router=<gateway>`.

Then upload the watcher and DHCP scripts from this folder:

```sh
# From your workstation (in keenetic-singbox/softether/):
scp -P 222 S05vpnclient        root@<router-lan-ip>:/opt/etc/init.d/S05vpnclient
scp -P 222 udhcpc.br2.script   root@<router-lan-ip>:/opt/etc/udhcpc.br2.script
scp -P 222 udhcpc.vpn.script   root@<router-lan-ip>:/opt/etc/udhcpc.vpn.script

# On the router:
ssh -p 222 root@<router-lan-ip> '
    chmod +x /opt/etc/init.d/S05vpnclient \
             /opt/etc/udhcpc.br2.script \
             /opt/etc/udhcpc.vpn.script
    /opt/etc/init.d/S05vpnclient restart
'
```

(If your dropbear doesn't ship SFTP, replace `scp` with the base64-pipe
trick used in the main installer — `cat foo | ssh ... 'base64 -d > foo'`.)

Within 10–20 s the watcher will:

1. `brctl addif br2 vpn_<profile>` — bridge SoftEther tunnel into the NDM bridge
2. `udhcpc -i br2` — get an IP from the HUB
3. Install `iptables -t nat POSTROUTING -o br2 SNAT --to-source <leased-ip>`
4. Walk every `dns-proxy route ... Bridge2` group and add
   `default via <hub-gateway> dev br2 table N` to that group's table

After that you can use `kn_gui` (below) or raw `dns-proxy route`
commands to bind any FQDN groups to `Bridge2`.

---

## Operating it

### Via kn_gui

1. Install v3.5.3+ from
   [keenetic-fqdn-manager releases](https://github.com/inlarin/keenetic-fqdn-manager/releases/latest).
2. Connect → in the interface dropdown pick **`Bridge2 — SoftEther
   vpn_<profile> via L2 bridge`**.
3. Tick the services you want → **Apply**.
4. Then on the router:

   ```sh
   ssh -p 222 root@<router-lan-ip> 'service S05vpnclient patch-now'
   ```

   This patches new groups' routing tables immediately (otherwise the
   watcher catches up within 60 s).

### Via NDM CLI manually

```
# Bind a group:
ndmc -c "dns-proxy route object-group <group_name> Bridge2 auto reject"

# Unbind:
ndmc -c "no dns-proxy route object-group <group_name> Bridge2"
```

### Manual full-refresh

```sh
ssh -p 222 root@<router-lan-ip> 'service S05vpnclient patch-now'
```

### Status check

```sh
ssh -p 222 root@<router-lan-ip> 'service S05vpnclient status'
```

Shows: TAP iface, br2 IP, bridge members, SNAT rule, all br2 routing
tables, watcher PID.

---

## Editing the SoftEther client config

To change server / password / HUB / add an account:

```sh
ssh -p 222 root@<router-lan-ip>
vpncmd /CLIENT localhost
# vpncmd interactive shell:
VPN Client> AccountList                              # all accounts
VPN Client> AccountGet <profile>                     # one account's detail
VPN Client> AccountServerCertSet <profile> /LOADCERT:...   # pin a cert
VPN Client> AccountDisconnect <profile>; AccountConnect <profile>   # reconnect

# Change server (same NicName):
VPN Client> AccountSet <profile> /SERVER:newhost:443 /HUB:NEW

# Change username (re-set password afterwards):
VPN Client> AccountUsernameSet <profile> /USERNAME:newuser
VPN Client> AccountPasswordSet <profile> /PASSWORD:... /TYPE:standard

VPN Client> exit
```

After any change — `AccountDisconnect <profile> / AccountConnect
<profile>` or `service S05vpnclient restart`.

---

## Common issues

| Symptom | Cause | Fix |
|---|---|---|
| `traceroute` hop 2 returns `host unreachable` | watcher hasn't yet placed the gateway in a new table; or `default dev br2` without `via` | `service S05vpnclient patch-now` |
| `no response after: include X.Y.Z` in kn_gui | NDM busy under watcher load | kn_gui v3.5.1+ retries; or stop watcher temporarily, apply, then start |
| Bridge2 not visible in NDM web UI | NDM detects subnet conflict with another tunnel — UI hides conflicts | cosmetic, not functional — kn_gui still sees it, dns-proxy still works |
| After reboot traceroute goes via WAN | watcher didn't start (`service S05vpnclient status`) — or `opkg disk storage:/` dropped from startup-config | `system configuration save` after `opkg disk storage:/`, verify with `show running-config \| grep opkg` |
| `Connection refused` on tcp/222 | dropbear didn't start → Entware didn't load | check `show running-config \| grep "opkg disk\|opkg initrc"` — both should be present; reinstall step 1 if not |
| SoftEther session retries forever | auth fail — wrong username/password/HUB | check with the server admin; some SoftEther deployments give separate creds for the native protocol vs SSTP |
| `No space left` on /opt | logs/cache piled up | `du -sh /opt/* 2>/dev/null \| sort -h \| tail` to find culprits; clean via `rm` or `opkg clean` |

---

## Full uninstall

```sh
# Via telnet:
ndmc -c "no interface Bridge2"
ndmc -c "no opkg disk"
ndmc -c "no opkg dns-override"
ndmc -c "no opkg initrc"
ndmc -c "no system mount storage:"
ndmc -c "erase storage:"
system configuration save
system reboot
```

After reboot the router is back to clean state, NAND wiped.

---

## References

- Watcher logic and rationale — comments in `S05vpnclient`
- kn_gui upstream — https://github.com/inlarin/keenetic-fqdn-manager
- SoftEther — https://github.com/SoftEtherVPN/SoftEtherVPN
- Keenetic CLI manual — https://help.keenetic.com/hc/en-us/articles/213965889
