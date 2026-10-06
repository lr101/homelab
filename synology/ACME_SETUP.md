# Synology Let's Encrypt with Cloudflare

This setup uses `acme.sh` on the Synology NAS with Cloudflare DNS-01 validation. This avoids exposing port 80 and works well when the NAS is behind a reverse proxy.

## 1. Install acme.sh

SSH into the NAS and become root:

```bash
sudo -i
```

Install acme.sh without its own cron job:

```bash
curl https://get.acme.sh | sh -s -- --no-cron --force
```

Verify:

```bash
/root/.acme.sh/acme.sh --version
```

## 2. Create a Cloudflare API token

Create a Cloudflare API token restricted to your DNS zone with:

```text
Zone → DNS → Edit
Zone → Zone → Read
```

Then export it:

```bash
export CF_Token='YOUR_CLOUDFLARE_TOKEN'
```

## 3. Request the certificate

Example for a wildcard certificate:

```bash
/root/.acme.sh/acme.sh \
  --issue \
  --dns dns_cf \
  -d example.com \
  -d '*.example.com' \
  --server letsencrypt
```

Cloudflare is used to automatically create the required DNS TXT records.

## 4. Deploy the certificate to DSM

Use the built-in Synology deploy hook:

```bash
export SYNO_USE_TEMP_ADMIN=1
export SYNO_LOCAL_HOSTNAME=1
export SYNO_CREATE=1
```

Then:

```bash
/root/.acme.sh/acme.sh \
  --deploy \
  -d example.com \
  --deploy-hook synology_dsm
```

Afterwards, assign the certificate to DSM and other services under:

```text
Control Panel → Security → Certificate
```

## 5. Automatic renewal

Create a DSM Task Scheduler task:

```text
Control Panel → Task Scheduler
User: root
Schedule: Daily
```

Use:

```bash
/root/.acme.sh/acme.sh --cron --home /root/.acme.sh \
  >> /var/log/acme-renew.log 2>&1
```

acme.sh checks daily but only renews certificates when necessary.

Check renewal logs with:

```bash
tail -100 /var/log/acme-renew.log
```
