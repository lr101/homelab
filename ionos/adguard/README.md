# DNS Setup

This document describes practical options for using Adguard Home as your local DNS resolver together with Docker containers running on the same host.

## Summary


Docker isolates container networks from the host and each other by design. Running a DNS resolver (Adguard Home) in a container on the same host introduces a challenge: containers must be able to reach the resolver's IP, but Docker's default network isolation and DNS config don't guarantee that when the resolver runs inside a container on the same host.

This guide explains recommended approaches, trade-offs, concrete commands and docker-compose examples to make Adguard Home usable as DNS for other containers.

In my opinion [Solution B](#solution-b-define-docker-wide) exposes less risks and has less configuration overhead and is therefore preferred. Both solutions are valid and therefore described here. 


## Solution A (Defining a specific Network)


Create a user-defined Docker network with a specific subnet, attach Adguard Home to that network with a static IP, and attach other containers to the same network. Configure those services to use the Adguard Home static IP as their DNS server. This keeps service reachability simple while avoiding `host` networking for every container.

Pros:

- Containers can reach Adguard Home using the static IP.
- Avoids `network_mode: host` for all containers.

Cons:

- Containers on that network can still reach each other (less isolation than fully separate networks).
- Requires managing static IPs and the dedicated network.

### Configuration (step-by-step)


1. Create a dedicated Docker network with a fixed subnet and gateway. Choose an IP range that does not collide with your host network:

```sh
docker network create --gateway 172.20.0.1 --subnet 172.20.0.0/24 backend
```

2. Example `docker-compose` snippet for Adguard Home with a static IPv4 address on the `backend` network:

```yaml
services:
   adguardhome:
    image: adguard/adguardhome
    container_name: adguardhome
    ports:
    - 53:53/tcp
    - 53:53/udp
    volumes:
    - ./workdir:/opt/adguardhome/work
    - ./confdir:/opt/adguardhome/conf
    restart: unless-stopped
    networks:
      backend:
        ipv4_address: 172.20.0.100

networks:
  backend:
    external: true
    ipam:
      config:
      - subnet: 172.20.0.0/16
```

3. Example `docker-compose` snippet for another service (Traefik) that uses Adguard Home as DNS by specifying `dns` and joining the same network:

```yaml
services:
  traefik:
    image: traefik:v3
    container_name: traefik
    restart: always
    ports:
      - "443:443"
      - "80:80"
    dns:
      - 172.20.0.100
    networks:
      - backend
    volumes:
      - ./traefik.yml:/etc/traefik/traefik.yaml:ro
      - ./dynamic:/etc/traefik/dynamic
      - ./certificates:/etc/traefik/certificates

networks:
  backend:
    external: true
    ipam:
      config:
      - subnet: 172.20.0.0/16
```

## Solution B (Define docker wide):

This setup forces all Docker containers to use your local AdGuard Home instance for DNS resolution by setting the default nameserver to the docker bridge ip.

Pro: Works for all containers

Con: Exposes port 53 to all networks on the device

### Configuration

Set the `/etc/docker/daemon.json` file to the docker bridge interface.

1. Create a adguard container that exposes its dns port to all interfaces:
  ```yml
  services:
    adguardhome:
      image: adguard/adguardhome
      container_name: adguardhome
      ports:
      - 53:53/tcp
      - 53:53/udp
      volumes:
      - ./workdir:/opt/adguardhome/work
      - ./confdir:/opt/adguardhome/conf
      restart: unless-stopped
  ```
2. Check the docker0 interface ip using `ip addr show docker0`. The default value should be `172.17.0.1`

  And update the file to something like this:
  ```json
  {
    "dns": ["172.17.0.1", "8.8.8.8"]
  }
  ```
2. Update the changes:
  ```
  sudo systemctl restart docker
  ```
  What this does is set the default DNS server for all containers to the docker bridge interface. Because adguard exposes its ports and binds to all interfaces (0.0.0.0:53) it also binds to the bridge interface. When we now allow all docker networks to access the bridge interface, the DNS server can be accessed by all containers.

3. Configure the firewall:
  ```
  sudo ufw allow from 172.16.0.0/12 to any port 53
  sudo ufw enable
  ```
  This allows the entire Docker private IP range (172.16.0.0/12) to access Port 53.

This setup is in my opinion a lot cleaner, as less configuration is needed and only port 53 instead of all devices are exposed to each other.