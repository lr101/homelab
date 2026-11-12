# Infisical on Dell

Start with the [setup, migration, and application onboarding guide](../../docs/INFISICAL.md).

This stack deliberately keeps its own bootstrap credentials in a local `.env`, independently of the manager. It exposes no host ports. Traefik serves `https://secrets.dell.lr-projects.de` to allowed VPN clients, using native Infisical authentication for both users and machine identities.

Before first deployment, from the repository root:

```sh
python3 scripts/infisical-secrets.py bootstrap
```

Back up `dell/infisical/.env` separately before starting the stack. Then, with the external `frontend` Docker network and Dell Traefik available:

```sh
cd dell/infisical
docker compose config --quiet
docker compose up -d
```

Do not regenerate the encryption key to repair a running installation. Restore it from the protected recovery copy.
