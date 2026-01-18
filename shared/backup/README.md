# Backup

This folder contains the deployment files and other utilise to easily setup backups for the different devices it is used on. The different services in the homelab_templates repository are already split by the hardware devices they are running on (for example the thinkpad or medion laptops). In every of these directories lies an ´autorestic.yml´ file which details the backup plan (keeping to the 3-2-1 rule). The [docker-compose.yml](./docker-compose.yml) file in this directory is the back-up software and configuration that then execute the backup plans. For this *autorestic* is used.

## Setup Autorestic

When adding a new device the following steps need to be taken:

1. Create a new folder in the root of this repo and create an `.autorestic.yml` file.
2. Add the backup plan. See [autorestic](https://autorestic.vercel.app/location) for more information.
3. Create a `.env` file in this directory, containing the path to the new location. For example:
```conf
# Needed for autorestic
DEVICE_FOLDER_PATH=/home/lr/homelab_templates/thinkpad # Path to the .autorestic.yml folder and backup location
SHARED_FOLDER_PATH=/home/lr/homelab_templates/shared   # (Optional) Path to the shared folder
LOCAL_BACKUP_PATH=/home/lr/.backup                     # (Optional) Local backup location
```
4. Add the backend configurations. This depends on your setup:

- **SFTP**
  - Mount .ssh key folder: `/home/{user}/.ssh:/root/.ssh:ro`
  - .autorestic.yml:
    ```yaml
    backends:
      <name>:
        type: sftp
        path: <host>:<path>
    ```

- **Rclone**
  - Mount rclone config: `./rclone.conf:/root/.config/rclone/:ro`
  - CLI Commands: Run the following to setup rclone:
    ```sh
    docker compose run --rm -v "$(pwd)":/rclone_config autorestic rclone config --config /rclone_config/rclone.conf
    ```
  - .autorestic.yml:
    ```yaml
    backends:
      <name>:
        type: rclone
        path: <remote>:<path>
    ```

- **Local**
  - Mount local backup dir: `/home/{user}/.backup:/backup`
  - .autorestic.yml:
    ```yaml
    backends:
      <name>:
        type: local
        path: /backup
    ``` 

5. Run 
```sh
sudo docker compose run --rm autorestic autorestic check
```
This will check if your configuration is configured correctly, initializes the backends and generates the encryption keys. To commit the `.autorestic.yml` file to git, make sure to copy the generated keys into a `.autorestic.env` file next to it. Remove the keys from the config and name the entries with the schema: `AUTORESTIC_<backend>_RESTIC_PASSWORD=...`.


1. Startup: `sudo docker compose up -d`

## Setup Hooks

The [hooks](./hooks/) contains useful scripts that are currently used by autorestic for backup purposes.

**Legacy - this was replaced by direct influx metric support**: To use the [influx-hook.py](./influx-hook.py) script, a `.env` file needs to be created at [hooks/.env](./hooks/.env) with the following values:

```env
INFLUXDB_URL=https://influx.medion.lr-projects.de
INFLUXDB_TOKEN=<token>
INFLUXDB_BUCKET=restic_backup
INFLUXDB_ORG=lr-projects
```

## Setup Influx Backup Metrics

To push metrics about the backups to influx, the following needs to be configured:

```yml
monitors:
  <monitor_name>:
    type: influx

locations:
  docker-tags:
    from:
    - <path>
    to:
    - <backend>
    monitors:
    - <monitor_name>

```

as well as adding the influx auth to `.autorestic.env`

```
AUTORESTIC_<monitor_name>_INFLUX_URL=
AUTORESTIC_<monitor_name>_INFLUX_TOKEN=
AUTORESTIC_<monitor_name>_INFLUX_ORG=
AUTORESTIC_<monitor_name>_INFLUX_BUCKET=
AUTORESTIC_<monitor_name>_SERVER_TAG=
```

The following information will be pushed to influx for every location **AND** backend that are configured in *to*:

The provided data:

| Metric Field           | Meaning                                         | Unit / Type | Notes                                     |
| ---------------------- | ----------------------------------------------- | ----------- | ----------------------------------------- |
| `added_size_bytes`     | Total size of newly added data in this snapshot | Bytes       | Useful for measuring daily backup growth  |
| `dirs_added`           | Number of new directories added                 | Count       | Indicates structural filesystem changes   |
| `dirs_changed`         | Number of directories that changed              | Count       | Shows churn in directory structure        |
| `dirs_unmodified`      | Number of directories unchanged                 | Count       | Helps calculate percentage of stable data |
| `duration_seconds`     | How long the restic backup ran                  | Seconds     | Primary performance metric                |
| `files_added`          | Number of new files added                       | Count       | Indicates new content created             |
| `files_changed`        | Number of files modified since last snapshot    | Count       | High values indicate active workloads     |
| `files_unmodified`     | Number of files unchanged                       | Count       | Helps estimate deduplication efficiency   |
| `processed_files`      | Total number of files scanned                   | Count       | Includes added + changed + unmodified     |
| `processed_size_bytes` | Total data size scanned during backup           | Bytes       | Represents the workload restic analyzed   |



The provided tags:

| Column         | Meaning                                | Example                                   |
| -------------- | -------------------------------------- | ----------------------------------------- |
| `backend`      | Your defined backend name              | `local`                                   |
| `exit_code`    | Exit code from restic run              | `0` = success, non-zero = error           |
| `location`     | Backup location                        | `docker-tags`                             |
| `server`       | Hostname of the machine backed up      | Useful for multi-host setups              |
| `snapshot_id`  | ID of the restic snapshot              | `8d30293d`                                |
| `tag`          | Restic tag(s) assigned to the snapshot | `tag:location:docker-tags`                |



## Restore:

Use the following to restore. If autorestic is already running `sudo docker container exec` can be used. Otherwise navigate to [shared/backup](./) and use `sudo docker compose run --rm`:
```sh
# List snapshot metadata
sudo docker container exec autorestic autorestic -c /data/.autorestic.yml exec -av -- snapshots

# Restore a specific snapshot
sudo docker container exec autorestic autorestic -c /data/.autorestic.yml restore -l <location> --from <backend> --to /data/<directory> <snapshot>

# Set correct access rights
sudo chown -R <user>:<user> $DEVICE_FOLDER_PATH/<directory>

# Move data to the correct directory
mv $DEVICE_FOLDER_PATH/<directory>/data/<location> /<some_location>
```

For example based on traefik running in the thinkpad folder:
```sh
sudo docker container exec autorestic autorestic -c /data/.autorestic.yml restore -l traefik --from nas --to /data/.restore f291f55a

sudo chown -R lr:lr ~/homelab_templates/thinkpad/.restore/

mv ~/homelab_templates/thinkpad/.restore/data/traefik ~/traefik-restored
```