#!/usr/bin/env python3
"""
Generate README.md with service tables based on docker-compose files.
One row per container service with annotations defining metadata.
"""

import yaml
from pathlib import Path
from typing import Dict, List, Tuple
import re
import argparse

REPO_ROOT = Path(__file__).parent.parent


def parse_docker_compose(file_path: Path) -> Dict:
    """Parse a docker-compose.yml file."""
    try:
        with open(file_path, 'r') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Error parsing {file_path}: {e}")
        return {}


def extract_annotation(annotations, key: str) -> str:
    """Extract a specific annotation value."""
    if not isinstance(annotations, list):
        return None
    
    for annotation in annotations:
        if isinstance(annotation, str) and annotation.startswith(f"de.lr-projects.{key}="):
            return annotation.split('=', 1)[1]
    
    return None


def extract_domains(service_data: Dict) -> list[str]:
    """Extract domain from traefik labels in a service."""
    labels = service_data.get('labels', [])
    
    if isinstance(labels, dict):
        label_list = [f"{k}={v}" for k, v in labels.items()]
    else:
        label_list = labels if isinstance(labels, list) else []
    
    matches = []
    for label in label_list:
        label_str = str(label)
        if 'traefik.http.routers' in label_str and 'rule=Host' in label_str:
            match = re.search(r'Host\(`([^`]+)`\)', label_str)
            if match:
                matches.append(match.group(1))
    
    return matches if matches else ["-"]


def get_watchtower_info(service_data: Dict) -> str:
    """Extract watchtower info from service labels."""
    labels = service_data.get('labels', [])
    
    if isinstance(labels, dict):
        label_list = [f"{k}={v}" for k, v in labels.items()]
    else:
        label_list = labels if isinstance(labels, list) else []
    
    for label in label_list:
        label_str = str(label)
        if 'com.centurylinklabs.watchtower.enable' in label_str:
            if 'false' not in label_str:
                return "monitored"
    
    return "manual"


def get_sso_status(service_data: Dict) -> str:
    """Report explicit proxy OIDC separately from declared application SSO."""
    labels = service_data.get('labels', [])
    if isinstance(labels, dict):
        labels = [f"{key}={value}" for key, value in labels.items()]
    if any('.middlewares=' in str(label) and
           'oidc-auth@file' in str(label).split('=', 1)[-1].split(',') for label in labels):
        return "proxy"
    declared = extract_annotation(service_data.get('annotations', []), 'sso')
    return "app (declared)" if declared and declared.lower() == 'true' else "-"


def parse_autorestic(device: str) -> Dict[str, list]:
    """Parse autorestic config to find which folders are backed up."""
    autorestic_path = REPO_ROOT / device / ".autorestic.yml"
    
    folder_to_backends = {}
    
    if not autorestic_path.exists():
        return folder_to_backends
    
    try:
        with open(autorestic_path, 'r') as f:
            data = yaml.safe_load(f) or {}
        
        locations = data.get('locations', {})
        for location_name, location_data in locations.items():
            if isinstance(location_data, dict):
                from_paths = location_data.get('from', [])
                to_backends = location_data.get('to', [])
                
                for from_path in from_paths:
                    # Normalize path - remove ./ prefix
                    normalized = from_path.replace('./', '')
                    folder_to_backends[normalized] = to_backends
    except Exception as e:
        print(f"Error parsing autorestic for {device}: {e}")
    
    return folder_to_backends


def get_backup_status(folder: str, folder_to_backends: Dict[str, list]) -> str:
    """Get backup status by checking if folder is in autorestic config."""
    # Check if this folder is in any autorestic location
    if folder_to_backends.get(folder):
        return "configured"
    return "-"


def extract_ports(service_data: Dict) -> str:
    """Extract and format host ports from service data."""
    ports = service_data.get('ports', [])
    if not ports:
        return ""
    
    exposed_ports = []
    for p in ports:
        if isinstance(p, str):
            # Handles "8080:80" or "127.0.0.1:8080:80"
            parts = p.split(':')
            # The host port is usually the second to last element
            if len(parts) >= 2:
                exposed_ports.append(parts[-2])
            else:
                exposed_ports.append(parts[0])
        elif isinstance(p, int):
            # Handles simple integer port
            exposed_ports.append(str(p))
        elif isinstance(p, dict):
            # Handles long-form: { published: 8080, target: 80 }
            published = p.get('published')
            if published:
                exposed_ports.append(str(published))
                
    return ", ".join(sorted(set(exposed_ports))) if exposed_ports else ""


def process_device(device: str) -> Tuple[str, List[Dict]]:
    """Process all services for a device and return table data."""
    device_path = REPO_ROOT / device
    folder_to_backends = parse_autorestic(device)
    shared_to_backends = parse_autorestic("shared")
    folder_to_backends.update(shared_to_backends)
    
    services_data = []
    
    def process_dir(directory: Path, is_shared: bool = False):
        if not directory.exists():
            return
            
        for service_dir in sorted(directory.iterdir()):
            if not service_dir.is_dir() or service_dir.name.startswith('.'):
                continue
            
            compose_file = next((service_dir / f for f in ["docker-compose.yml", "docker-compose.yaml"] if (service_dir / f).exists()), None)
            if not compose_file:
                continue
            
            compose_data = parse_docker_compose(compose_file)
            services = compose_data.get('services', {})
            
            for service_name, service_data in services.items():
                if not isinstance(service_data, dict): continue
                
                annotations = service_data.get('annotations', [])
                
                if is_shared:
                    deployed_on = extract_annotation(annotations, 'deployed_on')
                    if not deployed_on or device not in [d.strip() for d in deployed_on.split(',')]:
                        continue

                
                services_data.append({
                    'group': service_dir.name,
                    'name': service_name.replace('-', ' ').replace('_', ' ').title(),
                    'domains': extract_domains(service_data),
                    'ports': extract_ports(service_data),  # New field
                    'backup': get_backup_status(service_dir.name, folder_to_backends),
                    'update': get_watchtower_info(service_data),
                    'sso': get_sso_status(service_data),
                })

    process_dir(device_path)
    process_dir(REPO_ROOT / "shared", is_shared=True)
    
    return device, services_data


def generate_markdown_table(services: List[Dict], device: str) -> str:
    """Generate a markdown table with visual grouping and ports."""
    if not services:
        return ""
    
    # Table Header - Added 'Ports' column
    table = "| Group | Name  | Domain | Ports | Backup | Update | SSO |\n"
    table += "| :--- | :---  | :----- | :--- | :----: | :----: | :--: |\n"
    
    last_group = None
    for service in services:
        current_group = service['group']
        display_group = f"**{current_group}**" if current_group != last_group else ""
        
        domain_insert = "-"
        if service['domains'] != ["-"] and len(service['domains']) > 0:
            domain_links = []
            for domain in service['domains']:
                if '${DEVICE}' not in domain and "." in domain:
                    domain_text = domain.split(".")[0]
                    domain_links.append(f"[{domain_text}](https://{domain})")
                elif '${DEVICE}' in domain and "." in domain:
                    domain_text = domain.split(".")[0]
                    link = domain.replace('${DEVICE}', device)
                    domain_links.append(f"[{domain_text}](https://{link})")
            domain_insert = ", ".join(domain_links)
        ports = f"`{service['ports']}`" if service['ports'] else "-"
        
        table += (f"| {display_group} | {service['name']} | "
                  f"{domain_insert} | {ports} | {service['backup']} | {service['update']} | {service['sso']} |\n")
        
        last_group = current_group
    
    return table


def generate_tables(devices) -> str:
    """Generate all device tables."""
    tables_content = ""
    
    for device in devices:
        device_path = REPO_ROOT / device
        if not device_path.exists():
            continue
        
        device_title = device.capitalize()
        tables_content += f"### {device_title}\n\n"
        
        device_name, services = process_device(device)
        
        if services:
            table = generate_markdown_table(services, device_name)
            tables_content += table
            tables_content += "\n\n"
        else:
            tables_content += "No services configured.\n\n"
    
    return tables_content


def generate_readme(devices: list[str]) -> str:
    """Generate the complete README.md content using template."""
    template_path = REPO_ROOT / "scripts" / "README_TEMPLATE.md"
    
    if not template_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_path}")
    
    # Read template
    with open(template_path, 'r') as f:
        template = f.read()
    
    # Generate tables
    tables = generate_tables(devices)
    
    # Replace placeholder
    readme = template.replace("{{tables}}", tables)
    
    return readme


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Generate README.md with service tables")
    parser.add_argument('devices', nargs='*', default=[],
                        help=f"List of devices to process")
    args = parser.parse_args()

    readme_content = generate_readme(args.devices)
    output_path = REPO_ROOT / "README.md"
    
    with open(output_path, 'w') as f:
        f.write(readme_content)
    
    print(f"Generated README.md at {output_path}")


if __name__ == '__main__':
    main()
