import argparse
import json
import random
import socket
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

BUFFER_SIZE = 65535


class MessageType(str, Enum):
    RIP_UPDATE = "rip_update"
    RIP_REPORT = "rip_report"
    RIP_CONTROL = "rip_control"


class ControlAction(str, Enum):
    CONTINUE = "continue"
    STOP = "stop"


@dataclass(frozen=True)
class Link:
    left: str
    right: str
    metric: int


@dataclass
class Route:
    metric: int
    next_hop: str | None


class RipRouter(threading.Thread):
    def __init__(
        self,
        ip: str,
        rip_port: int,
        control_port: int,
        neighbours: dict[str, tuple[int, int]],
        *,
        host: str,
        max_steps: int,
        step_timeout: float,
        infinity: int,
        coordinator_port: int,
    ) -> None:
        super().__init__(daemon=True)
        self.ip = ip
        self.rip_port = rip_port
        self.control_port = control_port
        self.host = host
        self.neighbours = dict(neighbours)
        self.max_steps = max_steps
        self.step_timeout = step_timeout
        self.infinity = infinity
        self.coordinator_addr = (host, coordinator_port)
        self.last_step_by_neighbour: dict[str, int] = {}
        self.table = {ip: Route(0, None)}

        for neighbour_ip, (_, metric) in neighbours.items():
            self.table[neighbour_ip] = Route(min(metric, infinity), neighbour_ip)

        self.rip_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rip_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.rip_sock.bind((host, rip_port))

        self.control_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.control_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.control_sock.bind((host, control_port))

    def vector(self) -> dict[str, int]:
        return {dst: route.metric for dst, route in self.table.items()}

    def table_payload(self) -> dict[str, dict[str, int | str | None]]:
        return {
            dst: {"metric": route.metric, "next_hop": route.next_hop}
            for dst, route in self.table.items()
        }

    def apply_update(self, neighbour_ip: str, vector: dict[str, int]) -> bool:
        changed = False
        _, link_metric = self.neighbours[neighbour_ip]
        for dest, raw_metric in vector.items():
            if dest == self.ip:
                continue

            try:
                neighbour_metric = int(raw_metric)
            except (TypeError, ValueError):
                continue

            if neighbour_metric < 0:
                continue

            metric = self.infinity
            if neighbour_metric < self.infinity:
                metric = min(self.infinity, link_metric + neighbour_metric)

            current_route = self.table.get(dest)
            if current_route is None and metric >= self.infinity:
                continue

            if (
                current_route is None
                or metric < current_route.metric
                or current_route.next_hop == neighbour_ip
                and metric != current_route.metric
            ):
                next_hop = None if metric >= self.infinity else neighbour_ip
                self.table[dest] = Route(metric, next_hop)
                changed = True

        return changed

    def run(self) -> None:
        try:
            for step in range(1, self.max_steps + 1):
                changed = False
                packet = json.dumps(
                    {
                        "type": MessageType.RIP_UPDATE.value,
                        "source": self.ip,
                        "step": step,
                        "vector": self.vector(),
                    },
                    sort_keys=True,
                ).encode("utf-8")

                for _, (neighbour_port, _) in sorted(self.neighbours.items()):
                    self.rip_sock.sendto(packet, (self.host, neighbour_port))

                deadline = time.monotonic() + self.step_timeout
                while True:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        break

                    self.rip_sock.settimeout(timeout)
                    try:
                        data, _ = self.rip_sock.recvfrom(BUFFER_SIZE)
                    except socket.timeout:
                        break

                    try:
                        message = json.loads(data.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue

                    if message.get("type") != MessageType.RIP_UPDATE.value:
                        continue

                    neighbour_ip = message.get("source")
                    vector = message.get("vector")
                    if neighbour_ip not in self.neighbours or not isinstance(
                        vector, dict
                    ):
                        continue

                    try:
                        message_step = int(message.get("step", 0))
                    except (TypeError, ValueError):
                        continue

                    last_step = self.last_step_by_neighbour.get(neighbour_ip, 0)
                    if message_step <= last_step:
                        continue

                    self.last_step_by_neighbour[neighbour_ip] = message_step
                    changed = self.apply_update(neighbour_ip, vector) or changed

                report = json.dumps(
                    {
                        "type": MessageType.RIP_REPORT.value,
                        "source": self.ip,
                        "step": step,
                        "changed": changed,
                        "table": self.table_payload(),
                    },
                    sort_keys=True,
                ).encode("utf-8")
                self.control_sock.sendto(report, self.coordinator_addr)

                self.control_sock.settimeout(max(1.0, self.step_timeout * 10))
                while True:
                    try:
                        data, _ = self.control_sock.recvfrom(BUFFER_SIZE)
                    except socket.timeout:
                        return

                    try:
                        message = json.loads(data.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue

                    if (
                        message.get("type") != MessageType.RIP_CONTROL.value
                        or message.get("target") != self.ip
                    ):
                        continue

                    try:
                        control_step = int(message.get("step", 0))
                    except (TypeError, ValueError):
                        continue

                    if control_step != step:
                        continue

                    if message.get("action") == ControlAction.STOP.value:
                        return

                    break
        finally:
            self.rip_sock.close()
            self.control_sock.close()


def print_table(
    title: str,
    source_ip: str,
    all_ips: list[str],
    table: dict[str, dict[str, int | str | None]],
    infinity: int,
) -> None:
    print(title)
    print(
        f"{'[Source IP]':<16} {'[Destination IP]':<19} "
        f"{'[Next Hop]':<16} {'[Metric]':>8}"
    )
    for dest in all_ips:
        if dest == source_ip:
            continue

        route = table.get(dest, {})
        raw_metric = route.get("metric", infinity)
        try:
            route_metric = int(raw_metric)
        except (TypeError, ValueError):
            route_metric = infinity

        next_hop = route.get("next_hop")
        metric = "inf"
        if route_metric < infinity and isinstance(next_hop, str):
            metric = str(route_metric)
        else:
            next_hop = "-"

        print(f"{source_ip:<16} {dest:<19} {next_hop:<16} {metric:>8}")
    print()


parser = argparse.ArgumentParser()
parser.add_argument("--config", type=Path)
parser.add_argument("--routers", type=int)
parser.add_argument("--links", type=int)
parser.add_argument("--seed", type=int)
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--base-port", type=int, default=22000)
parser.add_argument("--steps", type=int)
parser.add_argument("--step-timeout", type=float, default=0.3)
parser.add_argument("--infinity", type=int, default=16)
parser.add_argument("--no-steps", action="store_true")
args = parser.parse_args()

if args.step_timeout <= 0:
    parser.error("--step-timeout must be greater than 0")
if args.infinity <= 1:
    parser.error("--infinity must be greater than 1")
if not 0 < args.base_port < 65536:
    parser.error("--base-port must be in range 1..65535")
if args.config is not None and (
    args.routers is not None or args.links is not None or args.seed is not None
):
    parser.error("--routers, --links and --seed can be used only without --config")

run_mode = "config" if args.config is not None else "random"
seed = None
seed_source = None

if args.config is not None:
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except OSError as e:
        parser.error(f"cannot read config: {e}")
    except json.JSONDecodeError as e:
        parser.error(f"invalid config JSON: {e}")

    if not isinstance(config, dict):
        parser.error("config root must be a JSON object")
    if not isinstance(config.get("routers"), list):
        parser.error('config field "routers" must be a list')
    if not isinstance(config.get("links"), list):
        parser.error('config field "links" must be a list')

    router_ips = [str(ip) for ip in config["routers"]]
    links = []

    for i, raw_link in enumerate(config["links"], start=1):
        if isinstance(raw_link, dict):
            if "left" not in raw_link or "right" not in raw_link:
                parser.error(f'config link #{i} must contain "left" and "right"')

            left = raw_link["left"]
            right = raw_link["right"]
            metric = raw_link.get("metric", 1)
        elif isinstance(raw_link, list):
            if len(raw_link) not in {2, 3}:
                parser.error(f"config link #{i} must contain 2 or 3 values")

            left = raw_link[0]
            right = raw_link[1]
            metric = raw_link[2] if len(raw_link) > 2 else 1
        else:
            parser.error(f"config link #{i} must be an object or a list")

        try:
            metric = int(metric)
        except (TypeError, ValueError):
            parser.error(f"config link #{i} metric must be an integer")

        links.append(Link(str(left), str(right), metric))
else:
    routers_count = args.routers if args.routers is not None else 5
    if routers_count < 2:
        parser.error("--routers must be at least 2 for random topology")

    seed = args.seed
    if seed is None:
        seed = random.SystemRandom().randrange(2**32)
        seed_source = "generated"
    else:
        seed_source = "provided"

    rng = random.Random(seed)
    router_ips = []
    while len(router_ips) < routers_count:
        ip = (
            f"10.{rng.randrange(1, 255)}."
            f"{rng.randrange(0, 256)}.{rng.randrange(1, 255)}"
        )
        if ip not in router_ips:
            router_ips.append(ip)

    max_links = routers_count * (routers_count - 1) // 2
    links_count = (
        args.links if args.links is not None else min(max_links, routers_count + 1)
    )
    if links_count < routers_count - 1 or links_count > max_links:
        parser.error(f"--links must be in range {routers_count - 1}..{max_links}")

    links = []
    used_pairs = set()
    for i in range(1, len(router_ips)):
        left = router_ips[i]
        right = router_ips[rng.randrange(i)]
        pair = tuple(sorted((left, right)))
        used_pairs.add(pair)
        links.append(Link(left, right, 1))

    while len(links) < links_count:
        left, right = rng.sample(router_ips, 2)
        pair = tuple(sorted((left, right)))
        if pair in used_pairs:
            continue

        used_pairs.add(pair)
        links.append(Link(left, right, 1))

if not router_ips:
    parser.error("topology must contain at least one router")
if len(set(router_ips)) != len(router_ips):
    parser.error("router IP addresses must be unique")
if args.base_port + 2 * len(router_ips) > 65535:
    parser.error("--base-port leaves not enough UDP ports for routers and coordinator")

router_set = set(router_ips)
for link in links:
    if link.left == link.right:
        parser.error(f"loop link is not allowed: {link.left}")
    if link.left not in router_set or link.right not in router_set:
        parser.error(f"link uses unknown router: {link.left} - {link.right}")
    if link.metric <= 0 or link.metric >= args.infinity:
        parser.error(f"link metric must be in range 1..{args.infinity - 1}")

router_ips = sorted(router_ips)
max_steps = args.steps if args.steps is not None else len(router_ips) + 1
if max_steps <= 0:
    parser.error("--steps must be greater than 0")

rip_port_by_ip = {ip: args.base_port + i for i, ip in enumerate(router_ips)}
coordinator_port = args.base_port + len(router_ips)
control_port_by_ip = {
    ip: args.base_port + len(router_ips) + 1 + i for i, ip in enumerate(router_ips)
}
neighbours_by_ip: dict[str, dict[str, tuple[int, int]]] = {ip: {} for ip in router_ips}

for link in links:
    left_neighbours = neighbours_by_ip[link.left]
    right_neighbours = neighbours_by_ip[link.right]

    if (
        link.right not in left_neighbours
        or link.metric < left_neighbours[link.right][1]
    ):
        left_neighbours[link.right] = (rip_port_by_ip[link.right], link.metric)
        right_neighbours[link.left] = (rip_port_by_ip[link.left], link.metric)

if run_mode == "config":
    print(f"Run mode: config, path={args.config}")
else:
    print(f"Run mode: random, seed={seed} ({seed_source})")

print(
    f"RIP simulation: routers={len(router_ips)}, links={len(links)}, "
    f"max_steps={max_steps}, rip_sockets={args.host}:{args.base_port}..{args.base_port + len(router_ips) - 1}, "
    f"coordinator={args.host}:{coordinator_port}, control_sockets={args.host}:{args.base_port + len(router_ips) + 1}..{args.base_port + 2 * len(router_ips)}"
)
print("Topology links:")
for link in sorted(links, key=lambda link: (link.left, link.right, link.metric)):
    print(f"  {link.left} <-> {link.right}, metric={link.metric}")
print("Routers exchange RIP tables and control messages through UDP sockets.\n")

coordinator_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
coordinator_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
coordinator_sock.bind((args.host, coordinator_port))
coordinator_sock.settimeout(max(1.0, args.step_timeout * 10))

router_threads = [
    RipRouter(
        ip,
        rip_port_by_ip[ip],
        control_port_by_ip[ip],
        neighbours_by_ip[ip],
        host=args.host,
        max_steps=max_steps,
        step_timeout=args.step_timeout,
        infinity=args.infinity,
        coordinator_port=coordinator_port,
    )
    for ip in router_ips
]

reports_by_ip: dict[str, dict] = {}
stop_reason = "max steps reached"
last_step = 0

try:
    for router_thread in router_threads:
        router_thread.start()

    for step in range(1, max_steps + 1):
        reports_by_ip = {}
        deadline = time.monotonic() + max(2.0, args.step_timeout * 20)
        while len(reports_by_ip) < len(router_ips):
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError(f"did not receive all reports for step {step}")

            coordinator_sock.settimeout(timeout)
            data, _ = coordinator_sock.recvfrom(BUFFER_SIZE)
            try:
                message = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue

            if message.get("type") != MessageType.RIP_REPORT.value:
                continue

            try:
                message_step = int(message.get("step", 0))
            except (TypeError, ValueError):
                continue

            source_ip = message.get("source")
            if message_step != step or source_ip not in rip_port_by_ip:
                continue

            reports_by_ip[source_ip] = message

        last_step = step
        if not args.no_steps:
            for ip in router_ips:
                print_table(
                    f"Simulation step {step} of router {ip}",
                    ip,
                    router_ips,
                    reports_by_ip[ip].get("table", {}),
                    args.infinity,
                )

        action = ControlAction.CONTINUE
        if not any(bool(report.get("changed")) for report in reports_by_ip.values()):
            action = ControlAction.STOP
            stop_reason = "converged"
        elif step == max_steps:
            action = ControlAction.STOP

        for ip, port in control_port_by_ip.items():
            control_packet = json.dumps(
                {
                    "type": MessageType.RIP_CONTROL.value,
                    "target": ip,
                    "step": step,
                    "action": action.value,
                },
                sort_keys=True,
            ).encode("utf-8")
            coordinator_sock.sendto(control_packet, (args.host, port))

        if action == ControlAction.STOP:
            break

    print(f"Simulation stopped: {stop_reason} after {last_step} step(s).\n")
    for ip in router_ips:
        print_table(
            f"Final state of router {ip} table:",
            ip,
            router_ips,
            reports_by_ip[ip].get("table", {}),
            args.infinity,
        )
finally:
    coordinator_sock.close()
    for router_thread in router_threads:
        router_thread.join(timeout=1.0)
