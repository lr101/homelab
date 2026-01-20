#!/bin/bash

# Configuration / Defaults
# You can change the filename here; the script adapts the service name automatically
WG_CONFIG="/etc/wireguard/wg0.conf"

DEFAULT_MTU=1400
DEFAULT_KEEPALIVE=25

# Check for root
if [[ $EUID -ne 0 ]]; then
   echo "Error: This script must be run as root." 
   exit 1
fi

function install_wireguard_client() {
    echo "--- Detecting Package Manager ---"
    
    if command -v apt-get &> /dev/null; then
        echo "Detected: apt (Debian/Ubuntu/Mint)"
        apt-get update && apt-get install -y wireguard qrencode
    elif command -v dnf &> /dev/null; then
        echo "Detected: dnf (Fedora/RHEL/CentOS)"
        dnf install -y wireguard-tools qrencode
    elif command -v pacman &> /dev/null; then
        echo "Detected: pacman (Arch/Manjaro)"
        pacman -S --noconfirm wireguard-tools qrencode
    elif command -v yum &> /dev/null; then
        echo "Detected: yum (Old CentOS)"
        yum install -y epel-release && yum install -y wireguard-tools qrencode
    elif command -v zypper &> /dev/null; then
        echo "Detected: zypper (OpenSUSE)"
        zypper install -y wireguard-tools qrencode
    elif command -v apk &> /dev/null; then
        echo "Detected: apk (Alpine)"
        apk add wireguard-tools qrencode
    else
        echo "Error: Could not detect a supported package manager."
        exit 1
    fi
    
    echo ""
    echo "Success! WireGuard is installed."
}

function create_client_config() {
    # 1. Pre-flight Checks & Dynamic Name Extraction
    if [ ! -f "$WG_CONFIG" ]; then
        echo "Error: Config file $WG_CONFIG not found."
        exit 1
    fi

    # Extract "wg0" from "/etc/wireguard/wg0.conf"
    WG_INTERFACE=$(basename "$WG_CONFIG" .conf)
    
    # 2. Detect Server Parameters
    SERVER_PUB_KEY=$(head -n 1 "$WG_CONFIG" | awk '{print $3}')
    if [[ -z "$SERVER_PUB_KEY" ]]; then
        echo "Error: Server public key not found in first line comment."
        exit 1
    fi

    # Read the full Address line
    # Example Line: Address = 10.217.236.1/32,10.217.236.0/24
    FULL_ADDRESS_LINE=$(grep "^Address" "$WG_CONFIG" | head -n 1 | cut -d'=' -f2)

    # Logic: Find the IP that has /32 specifically to use as the "Server IP"
    # We strip whitespace and split by comma
    SERVER_IP_32=""
    IFS=',' read -ra ADDR_PARTS <<< "$FULL_ADDRESS_LINE"
    for part in "${ADDR_PARTS[@]}"; do
        # Trim whitespace
        clean_part=$(echo "$part" | xargs)
        if [[ "$clean_part" == *"/32" ]]; then
            SERVER_IP_32=$(echo "$clean_part" | cut -d'/' -f1)
            break
        fi
    done

    # Fallback if no /32 found (grab the first IP and strip cidr)
    if [[ -z "$SERVER_IP_32" ]]; then
        echo "Warning: No /32 address found in config. Using first available IP."
        SERVER_IP_32=$(echo "${ADDR_PARTS[0]}" | xargs | cut -d'/' -f1)
    fi

    SERVER_PORT=$(grep "^ListenPort" "$WG_CONFIG" | awk '{print $3}')
    DETECTED_PUB_IP=$(curl -4 -s ifconfig.me)

    echo "--- Server Details Detected (Interface: $WG_INTERFACE) ---"
    echo "Internal IP: $SERVER_IP_32 (used for DNS)"
    echo "Public IP:   $DETECTED_PUB_IP"
    echo "Port:        $SERVER_PORT"
    echo "--------------------------------------------------------"

    # 3. User Inputs & Overwrites
    read -p "Enter Name for new Client: " CLIENT_NAME
    if [[ -z "$CLIENT_NAME" ]]; then echo "Name cannot be empty."; exit 1; fi

    echo ""
    echo "--- Configuration Parameters (Press Enter for Defaults) ---"
    
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

    # 4. Find Free IP
    # We assume the /32 IP belongs to a standard /24 range for allocation purposes
    BASE_IP_PREFIX=$(echo "$SERVER_IP_32" | cut -d'.' -f1-3)
    
    # Check used octets
    USED_OCTETS=$(grep "AllowedIPs" "$WG_CONFIG" | grep "$BASE_IP_PREFIX" | awk '{print $3}' | cut -d'/' -f1 | cut -d'.' -f4 | sort -n)

    CLIENT_OCTET=2
    # Ensure we don't pick the server's own octet
    SERVER_OCTET=$(echo "$SERVER_IP_32" | cut -d'.' -f4)
    
    while true; do
        # If octet equals server octet, skip
        if [ "$CLIENT_OCTET" -eq "$SERVER_OCTET" ]; then
            ((CLIENT_OCTET++))
            continue
        fi
        
        # If octet is in used list, skip
        if echo "$USED_OCTETS" | grep -q "^$CLIENT_OCTET$"; then
            ((CLIENT_OCTET++))
        else
            break
        fi
    done

    CLIENT_IP="$BASE_IP_PREFIX.$CLIENT_OCTET"
    echo "Allocating IP: $CLIENT_IP/32"

    # 5. Generate Keys
    CLIENT_PRIV_KEY=$(wg genkey)
    CLIENT_PUB_KEY=$(echo "$CLIENT_PRIV_KEY" | wg pubkey)
    CLIENT_PSK=$(wg genpsk)

    # 6. Write to Server Config
    echo "Adding peer to $WG_CONFIG..."
    cat <<EOT >> "$WG_CONFIG"

# Client: $CLIENT_NAME
[Peer]
PublicKey = $CLIENT_PUB_KEY
PresharedKey = $CLIENT_PSK
AllowedIPs = $CLIENT_IP/32
EOT

    # 7. Ask to Restart Service
    echo ""
    read -p "Do you want to restart the service (systemctl restart wg-quick@$WG_INTERFACE)? [y/N]: " RESTART_CONFIRM
    if [[ "$RESTART_CONFIRM" =~ ^[Yy]$ ]]; then
        echo "Restarting wg-quick@$WG_INTERFACE..."
        systemctl restart "wg-quick@$WG_INTERFACE.service"
        if [ $? -eq 0 ]; then
            echo "Service restarted successfully."
        else
            echo "Error restarting service."
        fi
    else
        echo "Skipping restart. You can restart manually later."
    fi

    # 8. Output Client Config
    echo ""
    echo "=========================================="
    echo "   CLIENT CONFIGURATION ($CLIENT_NAME)    "
    echo "=========================================="
    echo ""
    
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
    echo ""
    echo "=========================================="
}

# --- Main Menu ---
echo "WireGuard Manager"
echo "1) Create new Client Config (Run on Server)"
echo "2) Install WireGuard Software (Run on Client)"
read -p "Select an option [1]: " CHOICE
CHOICE=${CHOICE:-1}

case $CHOICE in
    1) create_client_config ;;
    2) install_wireguard_client ;;
    *) echo "Invalid option." ; exit 1 ;;
esac
