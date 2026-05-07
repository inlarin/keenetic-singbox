# SoftEther через NDM Bridge2 — установка и эксплуатация

Полная инструкция по установке Entware и SoftEther client во встроенную
память Keenetic-роутера (Netcraze Hopper 4G+ NC-2312, прошивка 5.0.10),
интеграции с NDM `dns-proxy route` через Bridge-обёртку и работе через
`kn_gui`. Всё проверено end-to-end, вы ходит ребут.

> Подробности «почему именно так» — см. соседний файл с проектной памятью
> [`project_softether_bridge_routing.md`](../../.claude/projects/...).
> Здесь — оперативный how-to.

---

## Архитектура (одной картинкой)

```
LAN-клиент (192.168.32.X)
  │
  │ DNS на 192.168.1.1 → NDM dnsmasq резолвит FQDN из object-group
  │ NDM кладёт IP в ipset _NDM_OGDN_4_@<group>
  │
  │ HTTP/etc. пакет к публичному IP
  ▼
Router (NDM)
  │ iptables -t mangle PREROUTING: match-set ipset → MARK 0xffffXXX
  │ ip rule:    fwmark 0xffffXXX → table N
  │ table N:    default via 10.X.0.1 dev br2     ← патчит наш watcher
  ▼
br2 (NDM Bridge2, Linux bridge)
  │ iptables -t nat POSTROUTING -o br2 → SNAT to 10.X.0.11
  ▼
vpn_redacted (softether TAP, член br2)
  │ vpnclient (Entware) → SSL на TCP/443
  ▼
vpn.example.com  (HUB="VPN", user "vpn-user-redacted")
  │
  ▼
интернет (Dallas exit IP)
```

---

## Предусловия (один раз через Web-GUI)

В Web-интерфейсе роутера → **Параметры системы → Изменение набора
компонентов** включить компоненты и применить (роутер ребутнётся):

- **OPKG** — пакетный менеджер
- **Ext-файловая система**
- **OpenVPN client** — на этой прошивке без него NDM не создаёт kernel-side
  TAP/TUN устройства корректно, и наша связка не работает
- *(опционально)* DNS-over-TLS / DNS-over-HTTPS proxy — независимо

После reboot проверить через telnet (`telnet admin@192.168.1.1`):
```
show version
# components: ...,openvpn,opkg,...   ← оба должны быть
```

---

## Шаг 1. Entware в NAND

Через NDM CLI (telnet, не через SSH/Entware — его ещё нет):
```
opkg disk storage:/ https://bin.entware.net/aarch64-k3.10/installer/aarch64-installer.tar.gz
system configuration save
system reboot
```

После ребута на TCP/222 поднимется dropbear (`root` / `keenetic`):
```
plink -ssh -P 222 -pw keenetic -batch root@192.168.1.1 'opkg update'
```

---

## Шаг 2. SoftEther client + базовые зависимости

```
ssh -p 222 root@192.168.1.1     # initial password: factory default (Keenetic docs)
# рекомендую passwd сменить сразу: passwd

opkg install softethervpn5-libs softethervpn5-client \
             iptables ipset ip-full
```

Размер ≈ 50 МБ. После установки `ls /opt/etc/init.d/` уже содержит
`S05vpnclient` (положен пакетом).

---

## Шаг 3. SoftEther account через `vpncmd`

> Особенность: `vpncmd /CMD ...` принимает аргументы как отдельные
> argv-токены. Не оборачивай команду целиком в кавычки — они попадут в
> имя команды и vpncmd ругнётся `Command not found`. Также в Git-Bash
> ставь префикс `MSYS_NO_PATHCONV=1` чтобы пути типа `/CMD` не
> преобразовались в Windows-формат.

```bash
# 1. Создать виртуальный TAP "dallas" → Linux получит vpn_redacted
vpncmd /CLIENT localhost /CMD NicCreate dallas

# 2. Создать VPN connection setting
vpncmd /CLIENT localhost /CMD AccountCreate dallas \
   /SERVER:vpn.example.com:443 \
   /HUB:VPN \
   /USERNAME:vpn-user-redacted \
   /NICNAME:dallas

# 3. Поставить пароль (стандартная password-аутентификация)
vpncmd /CLIENT localhost /CMD AccountPasswordSet dallas \
   /PASSWORD:<your_password> /TYPE:standard

# 4. Включить автоконнект при старте daemon (это важно — иначе после
#    reboot vpnclient запустится но НЕ подключится)
vpncmd /CLIENT localhost /CMD AccountStartupSet dallas

# 5. Подключиться сейчас
vpncmd /CLIENT localhost /CMD AccountConnect dallas
sleep 5
vpncmd /CLIENT localhost /CMD AccountStatusGet dallas
# Session Status должен быть "Connection Completed (Session Established)"
```

После этого в Linux есть `vpn_redacted` TAP с `<UP,LOWER_UP>`, но **без
IPv4** — DHCP делает наш watcher (см. шаг 5).

---

## Шаг 4. NDM Bridge2 (через telnet)

```
ndmc -c "interface Bridge2"
ndmc -c "interface Bridge2 description \"SoftEther vpn_redacted via L2 bridge\""
ndmc -c "interface Bridge2 role misc"
ndmc -c "interface Bridge2 security-level public"
ndmc -c "interface Bridge2 ip mtu 1500"
ndmc -c "interface Bridge2 ip tcp adjust-mss pmtu"
ndmc -c "interface Bridge2 ip global auto"
ndmc -c "interface Bridge2 up"
system configuration save
```

> NDM создаст kernel-side bridge `br2`. Включать в него `vpn_redacted` через
> `ndmc include` нельзя — `vpn_redacted` не NDM-known. Это сделает наш
> watcher через `brctl`.

---

## Шаг 5. Watcher: S05vpnclient + udhcpc.br2.script

Залить два файла из этой папки на роутер:

```bash
# Из локальной machine (где лежит этот repo):
pscp -scp -P 222 -pw <dropbear_pass> -batch \
     softether/S05vpnclient root@192.168.1.1:/opt/etc/init.d/S05vpnclient
pscp -scp -P 222 -pw <dropbear_pass> -batch \
     softether/udhcpc.br2.script root@192.168.1.1:/opt/etc/udhcpc.br2.script

# На роутере:
ssh -p 222 root@192.168.1.1 \
    'chmod +x /opt/etc/init.d/S05vpnclient /opt/etc/udhcpc.br2.script
     /opt/etc/init.d/S05vpnclient restart'
```

Через 10–20 сек watcher сделает:

1. `brctl addif br2 vpn_redacted` — softether-туннель в NDM-bridge
2. `udhcpc -i br2` — IP `192.168.30.X` от HUB
3. `iptables -t nat POSTROUTING -o br2 SNAT --to-source <ip>`
4. Пройдётся по всем `dns-proxy route ... Bridge2` группам и добавит
   `default via 10.X.0.1 dev br2 table N` в каждую table

Дальше — через kn_gui (см. ниже) или ручными `dns-proxy route` команды
ты привязываешь любые FQDN-группы к `Bridge2`.

---

## Эксплуатация

### Через kn_gui

1. Установить v3.5.3+ — `https://github.com/inlarin/keenetic-fqdn-manager/releases/latest`
2. Подключиться → в выпадающем списке выбрать **`Bridge2 — SoftEther
   vpn_redacted via L2 bridge`**
3. Поставить галочки на нужных сервисах → **Применить**
4. После apply — на роутере:

   ```bash
   ssh -p 222 root@192.168.1.1 'service S05vpnclient patch-now'
   ```

   Это патчит routing tables новых групп немедленно (иначе watcher
   сам подхватит за 60 сек).

### Через CLI вручную

```
# Привязать группу:
ndmc -c "dns-proxy route object-group <group_name> Bridge2 auto reject"

# Снять:
ndmc -c "no dns-proxy route object-group <group_name> Bridge2"
```

### Ручной refresh всего

```
ssh -p 222 root@192.168.1.1 'service S05vpnclient patch-now'
```

### Status check

```
ssh -p 222 root@192.168.1.1 'service S05vpnclient status'
```

Покажет: vpn_redacted, br2, члены bridge, SNAT-правило, все br2-таблицы
и PID watcher'а.

---

## Редактирование SoftEther client config

Если надо сменить сервер / пароль / HUB / создать дополнительный
аккаунт:

```bash
ssh -p 222 root@192.168.1.1
vpncmd /CLIENT localhost
# интерактивный shell vpncmd:
VPN Client> AccountList                              # все настройки
VPN Client> AccountGet dallas                        # детали одного
VPN Client> AccountServerCertSet dallas /LOADCERT:...# pin сертификата
VPN Client> AccountDisconnect dallas; AccountConnect dallas   # цикл
VPN Client> AccountDelete dallas; NicDelete dallas   # снести всё

# Server change (при том же НИКе):
VPN Client> AccountSet dallas /SERVER:newhost:443 /HUB:NEW

# Username change (после этого PasswordSet надо перевыставить):
VPN Client> AccountUsernameSet dallas /USERNAME:newuser
VPN Client> AccountPasswordSet dallas /PASSWORD:... /TYPE:standard

VPN Client> exit
```

После любых изменений — `AccountDisconnect / AccountConnect dallas`
или `service S05vpnclient restart` чтобы туннель пересобрался.

---

## Часто встречающиеся проблемы

| Симптом | Причина | Что делать |
|---|---|---|
| `tracert httpbin.org` хоп 2 = `192.168.99.1: host unreachable` | watcher не успел положить gateway в новую table; либо `default dev br2` без `via` | `service S05vpnclient patch-now` |
| `no response after: include X.Y.Z` в kn_gui | NDM busy под нагрузкой watcher | v3.5.1+ retry; или временно `service S05vpnclient stop`, apply, `start` |
| Bridge2 не виден в Web-UI Keenetic | NDM детектирует subnet conflict с SSTP0 (оба в 192.168.30.0/24) — Web-UI прячет конфликтующие | косметика, не функциональная — kn_gui всё равно видит, dns-proxy работает |
| После reboot tracert идёт через WAN | watcher не запустился (проверь `service S05vpnclient status`); или `opkg disk storage:/` пропал из startup-config | `system configuration save` после `opkg disk storage:/`, проверь `show running-config \| grep opkg` |
| `Connection refused` на 222 | dropbear не стартовал → Entware не загружается | `show running-config \| grep "opkg disk\|opkg initrc"` — должны быть оба; если нет — переустановить шаг 1 |
| SoftEther session retrying бесконечно | Auth fail — wrong username/password/HUB | сверь с админом сервера; для SoftEther у пользователя могут быть отдельные креды для нативного протокола (отличающиеся от SSTP) |
| NAND места мало (`No space left`) | На /opt накопились логи/кэш | `du -sh /opt/* 2>/dev/null \| sort -h \| tail` чтобы найти; чистить через `rm` или `opkg clean` |

---

## Откат всего

Если всё надо снести и начать заново:

```bash
# через telnet:
ndmc -c "no interface Bridge2"
ndmc -c "no opkg disk"
ndmc -c "no opkg dns-override"
ndmc -c "no opkg initrc"
ndmc -c "no system mount storage:"
ndmc -c "erase storage:"
system configuration save
system reboot
```

После ребута роутер вернётся в чистое состояние, NAND-раздел очищен.

---

## Дополнительные ссылки

- Watcher логика и почему так — `S05vpnclient` (комментарии в файле)
- kn_gui upstream — https://github.com/inlarin/keenetic-fqdn-manager
- SoftEther Developer Edition — https://github.com/SoftEtherVPN/SoftEtherVPN
- Keenetic CLI manual — https://help.keenetic.com/hc/en-us/articles/213965889
