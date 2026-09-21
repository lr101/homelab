# Traefik setup

## Crowdsec

TODO create token


## Docker Network

To remove docker network source masquerading, which can remove the source IP, set the following option:

```json
// /etc/docker/daemon.json
{
  "userland-proxy": false
}
```


