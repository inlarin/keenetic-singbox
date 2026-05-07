! Run via telnet to NDM (port 23) or `ndmc -c '<line>'`
interface OpkgTun0
interface OpkgTun0 description "sing-box hynet TUN"
interface OpkgTun0 ip address 172.19.0.1 255.255.255.255
interface OpkgTun0 ip global auto
interface OpkgTun0 ip mtu 1420
interface OpkgTun0 ip tcp adjust-mss pmtu
interface OpkgTun0 security-level public
interface OpkgTun0 up
ip route default 172.19.0.1 OpkgTun0
system configuration save
