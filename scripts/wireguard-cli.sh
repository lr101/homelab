#!/bin/bash

# Defaults
DEFAULT_MTU=1400
DEFAULT_KEEPALIVE=25
DEFAULT_PORT=51820
DEFAULT_SUBNET="10.217.236.0/24"

# Check for root
if [[ $EUID -ne 0 ]]; then
   echo "Error: This script must be run as root." 
   exit 1
fi

# --- Helper: Install Packages ---
function install_packages() {
    echo "--- Detecting Package Manager ---"
    if command -v apt-get &> /dev/null; then
        apt-get update && apt-get install -y wireguard qrencode openresolv
    elif command -v dnf &> /dev/null; then
        dnf install -y wireguard-tools qrencode
    elif command -v pacman &> /dev/null; then
        pacman -S --noconfirm wireguard-tools qrencode
    elif command -v yum &> /dev/null; then
        yum install -y epel-release && yum install -y wireguard-tools qrencode
    elif command -v zypper &> /dev/null; then
        zypper install -y wireguard-tools qrencode
    elif command -v apk &> /dev/null; then
        apk add wireguard-tools qrencode
    else
        echo "Error: Could not detect a supported package manager."
        return 1
    fi
    echo "Packages installed."
}

# --- OPTION 3: Install & Configure Server ---
function install_wireguard_server() {
    echo "=========================================="
    echo "      INSTALLING WIREGUARD SERVER"
    echo "=========================================="
    
    # 1. Install Software
    install_packages
    if [ $? -ne 0 ]; then exit 1; fi

    # 2. Enable IP Forwarding (Crucial for Server)
    echo ""
    echo "--- Enabling IP Forwarding ---"
    if ! grep -q "^net.ipv4.ip_forward=1" /etc/sysctl.conf; then
        echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
        sysctl -p
        echo "IP Forwarding enabled."
    else
        echo "IP Forwarding is already enabled."
    fi

    # 3. Detect & Ask for Variables
    # Detect default interface (internet facing)
    DETECTED_IFACE=$(ip -4 route ls | grep default | grep -Po '(?<=dev )(\S+)' | head -1)
    
    echo ""
    echo "--- Server Configuration ---"
    
    read -p "Enter VPN Subnet [$DEFAULT_SUBNET]: " USER_SUBNET
    VPN_SUBNET="${USER_SUBNET:-$DEFAULT_SUBNET}"
    
    # Calculate Server IP (assume .1 inside the subnet)
    # If subnet is 10.217.236.0/24 -> Base is 10.217.236 -> Server IP is 10.217.236.1
    BASE_IP_PREFIX=$(echo "$VPN_SUBNET" | cut -d'.' -f1-3)
    SERVER_IP="${BASE_IP_PREFIX}.1"
    
    read -p "Enter Server Listen Port [$DEFAULT_PORT]: " USER_PORT
    SERVER_PORT="${USER_PORT:-$DEFAULT_PORT}"

    read -p "Enter Network Interface (Internet facing) [$DETECTED_IFACE]: " USER_IFACE
    NET_IFACE="${USER_IFACE:-$DETECTED_IFACE}"

    read -p "Enter MTU [$DEFAULT_MTU]: " USER_MTU
    SERVER_MTU="${USER_MTU:-$DEFAULT_MTU}"

    read -p "Enter WireGuard Interface Name [wg0]: " WG_IFACE_INPUT
    WG_INTERFACE="${WG_IFACE_INPUT:-wg0}"
    WG_CONFIG="/etc/wireguard/${WG_INTERFACE}.conf"

    # 4. Generate Server Keys
    echo ""
    echo "Generating Server Keys..."
    SERVER_PRIV_KEY=$(wg genkey)
    SERVER_PUB_KEY=$(echo "$SERVER_PRIV_KEY" | wg pubkey)
    
    # 5. Write Config File
    echo "Writing configuration to $WG_CONFIG..."
    
    # Note: We use the specific format requested:
    # Address = SERVER_IP/32, SUBNET
    
    cat <<EOT > "$WG_CONFIG"
# PublicKey: $SERVER_PUB_KEY

[Interface]
Address = $SERVER_IP/32,$VPN_SUBNET
ListenPort = $SERVER_PORT
PrivateKey = $SERVER_PRIV_KEY
MTU = $SERVER_MTU
PostUp = ufw route allow in on $WG_INTERFACE out on $NET_IFACE
PostUp = iptables -t nat -I POSTROUTING -o $NET_IFACE -j MASQUERADE
PostUp = ip6tables -t nat -I POSTROUTING -o $NET_IFACE -j MASQUERADE
PostUp = iptables -I FORWARD -i $WG_INTERFACE -o $WG_INTERFACE -j ACCEPT
PostUp = ip6tables -I FORWARD -i $WG_INTERFACE -o $WG_INTERFACE -j ACCEPT

PreDown = ufw route delete allow in on $WG_INTERFACE out on $NET_IFACE
PreDown = iptables -t nat -D POSTROUTING -o $NET_IFACE -j MASQUERADE
PreDown = ip6tables -t nat -D POSTROUTING -o $NET_IFACE -j MASQUERADE
PreDown = iptables -D FORWARD -i $WG_INTERFACE -o $WG_INTERFACE -j ACCEPT
PreDown = ip6tables -D FORWARD -i $WG_INTERFACE -o $WG_INTERFACE -j ACCEPT

Table = auto
EOT

    chmod 600 "$WG_CONFIG"

    # 6. Start Service
    echo ""
    read -p "Do you want to enable and start the server now? [Y/n]: " START_NOW
    START_NOW=${START_NOW:-Y}

    if [[ "$START_NOW" =~ ^[Yy]$ ]]; then
        echo "Starting wg-quick@$WG_INTERFACE..."
        systemctl enable --now "wg-quick@$WG_INTERFACE"
        if [ $? -eq 0 ]; then
            echo "Server is UP and RUNNING."
            echo "Public Key: $SERVER_PUB_KEY"
        else
            echo "Error starting server. Check logs."
        fi
    fi
}

# --- OPTION 2: Install & Configure Client (Run on Client) ---
function install_wireguard_client() {
    install_packages
    if [ $? -ne 0 ]; then exit 1; fi
    
    echo ""
    echo "Success! WireGuard software installed."
    echo "--------------------------------------"

    read -p "Do you want to configure the interface now? [Y/n]: " CONFIGURE_NOW
    CONFIGURE_NOW=${CONFIGURE_NOW:-Y}

    if [[ "$CONFIGURE_NOW" =~ ^[Yy]$ ]]; then
        read -p "Enter interface name to create (default: wg0): " WG_IFACE_INPUT
        WG_IFACE=${WG_IFACE_INPUT:-wg0}
        WG_CONF_PATH="/etc/wireguard/${WG_IFACE}.conf"

        echo ""
        echo "Paste the configuration block below."
        echo "Press ENTER, then CTRL+D when you are finished."
        echo "------------------------------------------------"
        
        cat > "$WG_CONF_PATH"
        
        if [ -s "$WG_CONF_PATH" ]; then
            chmod 600 "$WG_CONF_PATH"
            echo ""
            echo "Configuration saved to $WG_CONF_PATH"

            read -p "Do you want to enable and start the service now? [Y/n]: " START_NOW
            START_NOW=${START_NOW:-Y}
            
            if [[ "$START_NOW" =~ ^[Yy]$ ]]; then
                echo "Starting wg-quick@$WG_IFACE..."
                systemctl enable --now "wg-quick@$WG_IFACE"
                if [ $? -eq 0 ]; then
                    echo "Service started successfully!"
                    wg show "$WG_IFACE"
                else
                    echo "Error starting service."
                fi
            fi
        else
            echo "Error: Configuration file is empty."
        fi
    fi
}

# --- OPTION 1: Create Client Config (Run on Server) ---
function create_client_config() {
    read -p "Enter WireGuard interface name (default: wg0): " WG_IFACE_INPUT
    WG_INTERFACE="${WG_IFACE_INPUT:-wg0}"
    WG_CONFIG="/etc/wireguard/${WG_INTERFACE}.conf"

    if [ ! -f "$WG_CONFIG" ]; then
        echo "Error: Config file $WG_CONFIG not found."
        exit 1
    fi
    
    # Detect Server Parameters
    SERVER_PUB_KEY=$(head -n 1 "$WG_CONFIG" | awk '{print $3}')
    if [[ -z "$SERVER_PUB_KEY" ]]; then
        echo "Error: Server public key not found in first line comment."
        exit 1
    fi

    # Read Address line to find /32
    FULL_ADDRESS_LINE=$(grep "^Address" "$WG_CONFIG" | head -n 1 | cut -d'=' -f2)
    SERVER_IP_32=""
    IFS=',' read -ra ADDR_PARTS <<< "$FULL_ADDRESS_LINE"
    for part in "${ADDR_PARTS[@]}"; do
        clean_part=$(echo "$part" | xargs)
        if [[ "$clean_part" == *"/32" ]]; then
            SERVER_IP_32=$(echo "$clean_part" | cut -d'/' -f1)
            break
        fi
    done

    if [[ -z "$SERVER_IP_32" ]]; then
        echo "Warning: No /32 address found. Using first available."
        SERVER_IP_32=$(echo "${ADDR_PARTS[0]}" | xargs | cut -d'/' -f1)
    fi

    SERVER_PORT=$(grep "^ListenPort" "$WG_CONFIG" | awk '{print $3}')
    DETECTED_PUB_IP=$(curl -4 -s ifconfig.me)

    echo ""
    echo "--- Server Details Detected (Interface: $WG_INTERFACE) ---"
    echo "Internal IP: $SERVER_IP_32"
    echo "Public IP:   $DETECTED_PUB_IP"
    echo "--------------------------------------------------------"

    read -p "Enter Name for new Client: " CLIENT_NAME
    if [[ -z "$CLIENT_NAME" ]]; then echo "Name cannot be empty."; exit 1; fi

    echo "--- Parameters (Enter for Defaults) ---"
    read -p "Endpoint IP/Host [$DETECTED_PUB_IP]: " USER_ENDPOINT
    FINAL_ENDPOINT="${USER_ENDPOINT:-$DETECTED_PUB_IP}"

    read -p "Endpoint Port [$SERVER_PORT]: " USER_PORT
    FINAL_PORT="${USER_PORT:-$SERVER_PORT}"

    read -p "DNS Server [$SERVER_IP_32]: " USER_DNS
    FINAL_DNS="${USER_DNS:-$SERVER_IP_32}"

    read -p "MTU [$DEFAULT_MTU]: " USER_MTU
    FINAL_MTU="${USER_MTU:-$DEFAULT_MTU}"

    read -p "PersistentKeepalive [$DEFAULT_KEEPALIVE]: " USER_KEEPALIVE
    FINAL_KEEPALIVE="${USER_KEEPALIVE:-$DEFAULT_KEEPALIVE}"

    # Find Free IP
    BASE_IP_PREFIX=$(echo "$SERVER_IP_32" | cut -d'.' -f1-3)
    USED_OCTETS=$(grep "AllowedIPs" "$WG_CONFIG" | grep "$BASE_IP_PREFIX" | awk '{print $3}' | cut -d'/' -f1 | cut -d'.' -f4 | sort -n)

    CLIENT_OCTET=2
    SERVER_OCTET=$(echo "$SERVER_IP_32" | cut -d'.' -f4)
    
    while true; do
        if [ "$CLIENT_OCTET" -eq "$SERVER_OCTET" ]; then
            ((CLIENT_OCTET++))
            continue
        fi
        if echo "$USED_OCTETS" | grep -q "^$CLIENT_OCTET$"; then
            ((CLIENT_OCTET++))
        else
            break
        fi
    done

    CLIENT_IP="$BASE_IP_PREFIX.$CLIENT_OCTET"
    echo "Allocating IP: $CLIENT_IP/32"

    # Generate Keys
    CLIENT_PRIV_KEY=$(wg genkey)
    CLIENT_PUB_KEY=$(echo "$CLIENT_PRIV_KEY" | wg pubkey)
    CLIENT_PSK=$(wg genpsk)

    # Write to Server Config
    echo "Adding peer to $WG_CONFIG..."
    cat <<EOT >> "$WG_CONFIG"

# Client: $CLIENT_NAME
[Peer]
PublicKey = $CLIENT_PUB_KEY
PresharedKey = $CLIENT_PSK
AllowedIPs = $CLIENT_IP/32
EOT

    # Restart Service
    echo ""
    read -p "Restart service (systemctl restart wg-quick@$WG_INTERFACE)? [y/N]: " RESTART_CONFIRM
    if [[ "$RESTART_CONFIRM" =~ ^[Yy]$ ]]; then
        systemctl restart "wg-quick@$WG_INTERFACE.service"
        echo "Service restarted."
    fi

    # Output Client Config
    echo ""
    echo "=========================================="
    echo "   CLIENT CONFIGURATION ($CLIENT_NAME)    "
    echo "=========================================="
    
    cat <<EOT
[Interface]
Address = $CLIENT_IP/32
PrivateKey = $CLIENT_PRIV_KEY
DNS = $FINAL_DNS
MTU = $FINAL_MTU

[Peer]
PublicKey = $SERVER_PUB_KEY
PresharedKey = $CLIENT_PSK
Endpoint = $FINAL_ENDPOINT:$FINAL_PORT
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = $FINAL_KEEPALIVE
EOT
    echo "=========================================="
}

# --- Main Menu ---
echo "WireGuard Manager"
echo "1) Create new Client Config (Run on Server)"
echo "2) Install WireGuard Software (Run on Client)"
echo "3) Install WireGuard Server (Fresh Install)"
read -p "Select an option [1]: " CHOICE
CHOICE=${CHOICE:-1}

case $CHOICE in
    1) create_client_config ;;
    2) install_wireguard_client ;;
    3) install_wireguard_server ;;
    *) echo "Invalid option." ; exit 1 ;;
esac
