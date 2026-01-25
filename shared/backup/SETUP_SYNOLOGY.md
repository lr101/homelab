Synology NAS SFTP & SSH Key Setup Guide

This guide details how to enable SFTP on a Synology NAS, create a dedicated backup user, generate SSH keys, and configure your local machine for passwordless authentication. This setup is ideal for automated backups (e.g., using rsync or borg).


## 1. Enable SFTP on Synology

1. Log in to your Synology DSM.
2. Go to Control Panel > File Services.
3. Click on the FTP tab.
4. Scroll down to the SFTP section.
5. Check the box Enable SFTP service.
6. Click Apply.

## 2. Create a Dedicated Backup User

It is best practice to use a dedicated user for backups rather than your admin account.

1. Go to Control Panel > User & Group.
2. Click Create.
3. Name: Enter a name (e.g., backup_user).
4. Password: Set a strong password.
5. Group: Assign to the users group (avoid administrators unless necessary) or backup group when it exists
6. Shared Folder Permissions: Check Read/Write for the specific folder you want to back up to as well as the users home folder (for ssh keys). Create the backup folder when necessary (e.g., /volume1/backups).
7. Applications: Ensure SFTP (and FTP) is set to Allow.
8. Finish the wizard.

## 3. Generate SSH Keys (On Client)

Perform these steps on the computer (client) that will be sending the backups.

1. Open your terminal.
2. Run the key generation command:
    ```sh
    ssh-keygen -t ed25519 -f ~/.ssh/synology_nas
    ```
3. You now have two files in ~/.ssh/:
    - `synology_nas` (Private key - Keep Secret)
    - `synology_nas.pub` (Public key - Share with NAS)

## 4. Authorize the Key on Synology

You need to place the content of your public key into the NAS user's authorized_keys file. Since ssh-copy-id can be tricky with Synology permissions, manual setup is often safer.

1. Copy your public key content on your computer:
    ```sh
    cat ~/.ssh/synology_backup_key.pub
    ```
    (Copy the output string starting with ssh-ed25519...)

2. SSH into your NAS using your admin account (or use File Station if you prefer GUI):
    ```sh
    ssh admin@YOUR_NAS_IP 
    ```
3. Navigate to the backup user's home:
    ```sh
    cd /var/services/homes/backup_user
    ```

4. Create the .ssh directory and file:
    ```
    mkdir .ssh
    nano .ssh/authorized_keys
    ```
    (Paste the public key you copied earlier. Save by pressing Ctrl+O, Enter, then Ctrl+X).

5. Set Critical Permissions: Synology is very strict. If these permissions are wrong, authentication will fail.
    ```
    sudo chown -R backup_user:users .ssh
    sudo chmod 700 .ssh
    sudo chmod 600 .ssh/authorized_keys
    ```

## 5. Configure SSH Config File (Client)


1. On your computer, open (or create) your config file:
    ```
    nano ~/.ssh/config
    ```

2. Add the following block:

    ```
    Host synology-backup
        HostName 192.168.1.100      # Replace with your NAS IP or DDNS
        User backup_user            # The user created in Step 2
        Port 22                     # Or your custom SFTP port
        IdentityFile ~/.ssh/synology_backup_key
    ```

3. Save and close the file.

## 6. Test the Connection

You should now be able to connect without entering a password.

1. Test SSH/SFTP access:
    ```
    ssh synology-backup
    ```
    If you see a prompt strictly for a shell, or if it connects and closes (depending on shell permissions), the auth worked.

2. Test with SFTP directly:
    ```
    sftp synology-backup
    ```
    You should see Connected to synology-backup and an sftp> prompt.

If this does not work make sure the home folder, .ssh folder and authorized_key file have the correct permissions and are owned by the correct user. An indicator for such a problem is when the ssh config asks for a password prompt.