# Homelab Templates

This repository contains setup templates and configuration files for various applications running in my home lab environment. It serves as a centralized location for maintaining and versioning infrastructure-as-code configurations.

## Applications

### Thinkpad

| Group | Name  | Domain | Ports | Backup | Update | SSO |
| :--- | :---  | :----- | :--- | :----: | :----: | :--: |
| **garage** | Garage | [minio](https://minio.lr-projects.de) | - | - | monitored | - |
| **glance** | Glance | [home](https://home.thinkpad.lr-projects.de) | - | configured | monitored | - |
| **ha** | Homeassistant | [ha](https://ha.thinkpad.lr-projects.de) | - | configured | monitored | app (declared) |
|  | Mariadb | - | `3306` | configured | monitored | - |
| **iwi_bulletinboard** | App | - | - | - | monitored | - |
| **monaserver** | Stick It Server | [stick-it](https://stick-it.thinkpad.lr-projects.de), [stick-it-admin](https://stick-it-admin.thinkpad.lr-projects.de) | - | configured | manual | - |
|  | Rustfs | [minio](https://minio.lr-projects.de), [s3-admin](https://s3-admin.thinkpad.lr-projects.de) | - | configured | manual | - |
| **postgis** | Db | - | - | configured | monitored | - |
| **tempserver** | Tempserver | [temppi](https://temppi.thinkpad.lr-projects.de) | `8081` | configured | manual | - |
| **traefik** | Reverse Proxy | [traefik](https://traefik.thinkpad.lr-projects.de) | `443, 80` | configured | monitored | proxy |
|  | Socket Proxy | - | - | configured | manual | - |
| **backup** | Autorestic | - | - | - | monitored | - |
| **telegraf** | Telegraf | - | - | - | monitored | - |
| **watchtower** | Watchtower | - | - | - | manual | - |


### Medion

| Group | Name  | Domain | Ports | Backup | Update | SSO |
| :--- | :---  | :----- | :--- | :----: | :----: | :--: |
| **actual-budget** | Actual Server | [budget](https://budget.medion.lr-projects.de) | - | configured | monitored | - |
| **adguard** | Adguardhome | [dns](https://dns.medion.lr-projects.de) | `53` | configured | monitored | proxy |
|  | Adguard Exporter | - | - | configured | monitored | - |
| **hiring_scraper** | Web | [jobs](https://jobs.medion.lr-projects.de) | - | configured | manual | - |
|  | Worker | - | - | configured | manual | - |
|  | Scheduler | - | - | configured | manual | - |
|  | Db | - | - | configured | manual | - |
|  | Redis | - | - | configured | manual | - |
| **immich-app** | Immich Server | [immich](https://immich.medion.lr-projects.de) | - | configured | monitored | app (declared) |
|  | Immich Machine Learning | - | - | configured | monitored | - |
|  | Redis | - | - | configured | monitored | - |
|  | Database | - | - | configured | monitored | - |
|  | Immich Kiosk | [diashow](https://diashow.medion.lr-projects.de) | - | configured | monitored | app (declared) |
| **jellyfin** | Jellyfin | [jellyfin](https://jellyfin.medion.lr-projects.de) | `8096` | configured | monitored | app (declared) |
| **logging** | Influxdb | [influx](https://influx.medion.lr-projects.de) | - | configured | monitored | - |
|  | Grafana | [grafana](https://grafana.medion.lr-projects.de) | - | configured | monitored | app (declared) |
|  | Prometheus | [prometheus](https://prometheus.medion.lr-projects.de) | - | configured | monitored | proxy |
|  | Uptime Kuma | [uptime](https://uptime.medion.lr-projects.de) | - | configured | monitored | proxy |
| **nextcloud** | Nextcloud | [nextcloud](https://nextcloud.medion.lr-projects.de) | - | configured | monitored | app (declared) |
|  | Elasticsearch | - | - | configured | manual | - |
|  | Database | - | - | configured | monitored | - |
|  | Redis | - | - | configured | monitored | - |
|  | Euro Office | [office](https://office.lr-projects.de) | - | configured | monitored | - |
| **pocket-id** | Pocket Id | [sso](https://sso.medion.lr-projects.de) | - | configured | monitored | - |
| **stick-it-homepage** | App | [stick-it-home](https://stick-it-home.medion.lr-projects.de) | - | configured | manual | - |
| **traefik** | Reverse Proxy | [traefik](https://traefik.medion.lr-projects.de) | `443, 80` | configured | monitored | proxy |
|  | Socket Proxy | - | - | configured | manual | - |
| **backup** | Autorestic | - | - | - | monitored | - |
| **telegraf** | Telegraf | - | - | - | monitored | - |
| **watchtower** | Watchtower | - | - | - | manual | - |


### Ionos

| Group | Name  | Domain | Ports | Backup | Update | SSO |
| :--- | :---  | :----- | :--- | :----: | :----: | :--: |
| **adguard** | Adguardhome | [dns](https://dns.ionos.lr-projects.de) | `53` | configured | monitored | proxy |
|  | Adguard Exporter | - | - | configured | monitored | - |
| **traefik** | Traefik | - | `443, 80` | configured | monitored | app (declared) |
|  | Crowdsec | - | - | configured | monitored | - |
|  | Socket Proxy | - | - | configured | manual | - |
| **backup** | Autorestic | - | - | - | monitored | - |
| **telegraf** | Telegraf | - | - | - | monitored | - |
| **watchtower** | Watchtower | - | - | - | manual | - |


### Dell

| Group | Name  | Domain | Ports | Backup | Update | SSO |
| :--- | :---  | :----- | :--- | :----: | :----: | :--: |
| **infisical** | Infisical | [secrets](https://secrets.dell.lr-projects.de) | - | configured | manual | - |
|  | Db | - | - | configured | manual | - |
|  | Redis | - | - | configured | manual | - |
| **t3-code** | T3 Code | [code](https://code.dell.lr-projects.de) | - | configured | monitored | proxy |
| **traefik** | Reverse Proxy | [traefik](https://traefik.dell.lr-projects.de) | `443, 80` | configured | monitored | proxy |
|  | Socket Proxy | - | - | configured | manual | - |
| **backup** | Autorestic | - | - | - | monitored | - |




The inventory describes configuration, not live verification. **Backup: configured** means a source folder and destination are declared; external mounts, exclusions, and successful restores still need checking. **Update: monitored** means Watchtower opt-in; the shared Watchtower runs in notification-only mode. **SSO: proxy** means an explicit OIDC middleware label; **app (declared)** is application SSO metadata, not a verified login test.

See [operations and rollout](docs/OPERATIONS.md) for prerequisites, backup coverage, restore drills, and update procedures.

## Secret management

[Infisical on Dell](docs/INFISICAL.md) manages per-host deployment secrets. The guide covers initial setup, adding applications, migrating existing values, deployment commands, and offline recovery. Bootstrap credentials stay local; no live secrets are committed.

## Homelab setup

The homelab uses a WireGuard VPN hosted on an Ionos VPS with a public IP to securely connect remote devices (thinkpad, medion, NAS, and mobile devices) in a private network. Services run on the thinkpad and medion laptops in my parents basement.

**Internet Access:** External clients connect via HTTPS to the public IP, where a Traefik reverse proxy routes requests to services running on the internal devices over the encrypted VPN tunnel.

**Internal Access:** Clients connected to the VPN can directly access services without going through the reverse proxy, providing access to services that are not reachable from the outside.

Network Architecture:

```mermaid
graph TB
    subgraph Internet["🌐 Internet"]
        Users["External Users<br/>VPN Clients"]
    end
    
    subgraph Ionos["Ionos VPS - Public IP"]
        PublicIP["Public IP Address"]
        WGServer["WireGuard Server"]
        TraefikProxy["Traefik Reverse Proxy"]
    end
    
    subgraph VPN["🔒 WireGuard VPN Network"]
        TP["💻 Thinkpad<br/>Services: glance, ha,<br/>tempserver, postgis"]
        MD["💾 Medion<br/>Services: immich, jellyfin,<br/>nextcloud, adguard + more"]
        NAS["📦 NAS<br/>Backups & Storage"]
        Mobile["📱 Mobile Devices"]
    end
    
    Users -->|HTTPS| PublicIP
    PublicIP --> TraefikProxy
    TraefikProxy -->|Routes Services| WGServer
    WGServer -->|Encrypted Tunnel| VPN
    Mobile -.->|VPN Connection| WGServer
```




## Purpose

The goal of this repository is to:
- Maintain version control of configuration files
- Document setup procedures
- Backup setup procedures
- Share deployment configs with friends :)

## Getting Started

Each application folder contains the used setup (mostly docker-compose.yml) and the used configuration files with exempted secrets.
