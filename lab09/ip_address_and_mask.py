import argparse
import ipaddress
import json
import socket
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--target-host", default="8.8.8.8")
args = parser.parse_args()
try:
    target_ip = socket.gethostbyname(args.target_host)
except OSError:
    target_ip = None

route_completed = None
if target_ip is not None:
    try:
        route_completed = subprocess.run(
            ["ip", "-j", "-4", "route", "get", target_ip],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        pass

local_ip = None
iface_name = None
if route_completed is not None and route_completed.returncode == 0:
    try:
        routes = json.loads(route_completed.stdout)
    except json.JSONDecodeError:
        routes = []

    if routes:
        route = routes[0]
        local_ip = route.get("prefsrc") or route.get("src")
        iface_name = route.get("dev")

iface = None
if local_ip is not None and iface_name is not None:
    try:
        addr_completed = subprocess.run(
            ["ip", "-j", "-4", "addr", "show", "dev", iface_name],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        addr_completed = None

    if addr_completed is not None and addr_completed.returncode == 0:
        try:
            interfaces = json.loads(addr_completed.stdout)
        except json.JSONDecodeError:
            interfaces = []

        for cur_iface in interfaces:
            if cur_iface.get("ifname") != iface_name:
                continue

            for addr_info in cur_iface.get("addr_info", []):
                if addr_info.get("family") != "inet":
                    continue
                if addr_info.get("local") != local_ip:
                    continue

                prefixlen = addr_info.get("prefixlen")
                try:
                    network = ipaddress.ip_network(f"0.0.0.0/{prefixlen}")
                except ValueError:
                    continue

                iface = {
                    "name": iface_name,
                    "ip": local_ip,
                    "mask": str(network.netmask),
                }
                break

            if iface is not None:
                break

if iface is None:
    if local_ip is None:
        print("IP address: could not be determined")
    else:
        print(f"IP address: {local_ip}")

    print("Network mask: could not be determined")
    sys.exit(1)

print(f"IP address: {iface['ip']}")
print(f"Network mask: {iface['mask']}")
