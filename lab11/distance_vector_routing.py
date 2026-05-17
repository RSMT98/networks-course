import argparse
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass

INF = 10**9
BASE_LINKS = (
    (0, 1, 1),
    (1, 2, 1),
    (2, 3, 2),
    (0, 3, 7),
    (0, 2, 3),
)

EXPECTED_INITIAL_COSTS = {
    0: {0: 0, 1: 1, 2: 2, 3: 4},
    1: {0: 1, 1: 0, 2: 1, 3: 3},
    2: {0: 2, 1: 1, 2: 0, 3: 2},
    3: {0: 4, 1: 3, 2: 2, 3: 0},
}

EXPECTED_AFTER_1_2_INCREASE = {
    0: {0: 0, 1: 1, 2: 3, 3: 5},
    1: {0: 1, 1: 0, 2: 4, 3: 6},
    2: {0: 3, 1: 4, 2: 0, 3: 2},
    3: {0: 5, 1: 6, 2: 2, 3: 0},
}

EXPECTED_AFTER_0_3_DECREASE = {
    0: {0: 0, 1: 1, 2: 3, 3: 2},
    1: {0: 1, 1: 0, 2: 4, 3: 3},
    2: {0: 3, 1: 4, 2: 0, 3: 2},
    3: {0: 2, 1: 3, 2: 2, 3: 0},
}


@dataclass
class Route:
    cost: int
    next_hop: int | None


class Router:
    def __init__(self, name: int) -> None:
        self.name = name
        self.neighbours: dict[int, int] = {}
        self.neighbour_vectors: dict[int, dict[int, int]] = {}
        self.table: dict[int, Route] = {name: Route(0, None)}

    def set_link_cost(self, neighbour: int, cost: int) -> None:
        if cost <= 0:
            raise ValueError("link cost must be greater than 0")

        self.neighbours[neighbour] = cost

    def vector(self) -> dict[int, int]:
        return {node: route.cost for node, route in self.table.items()}

    def apply_vector(self, neighbour: int, vector: dict[int, int]) -> bool:
        self.neighbour_vectors[neighbour] = dict(vector)
        return self.recalc()

    def recalc(self) -> bool:
        old_table = {
            node: (route.cost, route.next_hop) for node, route in self.table.items()
        }
        new_table = {self.name: Route(0, None)}

        for neighbour, link_cost in sorted(self.neighbours.items()):
            route = new_table.get(neighbour)
            if route is None or link_cost < route.cost:
                new_table[neighbour] = Route(link_cost, neighbour)

        for neighbour, vector in sorted(self.neighbour_vectors.items()):
            if neighbour not in self.neighbours:
                continue

            link_cost = self.neighbours[neighbour]
            for node, neighbour_cost in sorted(vector.items()):
                if node == self.name or neighbour_cost >= INF:
                    continue

                cost = link_cost + neighbour_cost
                route = new_table.get(node)
                cur_next_hop = INF
                if route is not None and route.next_hop is not None:
                    cur_next_hop = route.next_hop

                if (
                    route is None
                    or cost < route.cost
                    or (cost == route.cost and neighbour < cur_next_hop)
                ):
                    new_table[node] = Route(cost, neighbour)

        self.table = dict(sorted(new_table.items()))
        return old_table != {
            node: (route.cost, route.next_hop) for node, route in self.table.items()
        }


class DistanceVectorNetwork:
    def __init__(self, links: tuple[tuple[int, int, int], ...]) -> None:
        nodes = sorted({node for link in links for node in link[:2]})
        self.routers = {node: Router(node) for node in nodes}
        self.packets: deque[tuple[int, int, dict[int, int]]] = deque()
        self.sent_packets = 0

        for l, r, cost in links:
            self.routers[l].set_link_cost(r, cost)
            self.routers[r].set_link_cost(l, cost)

        for router in self.routers.values():
            router.recalc()

    def send_vector_to_neighbours(self, src: int) -> None:
        vector = self.routers[src].vector()
        for neighbour in sorted(self.routers[src].neighbours):
            self.packets.append((src, neighbour, dict(vector)))
            self.sent_packets += 1

    def send_all_vectors(self) -> None:
        for src in sorted(self.routers):
            self.send_vector_to_neighbours(src)

    def process_updates_until_stable(self) -> int:
        processed_packets = 0
        while self.packets:
            src, target, vector = self.packets.popleft()
            processed_packets += 1
            if self.routers[target].apply_vector(src, vector):
                self.send_vector_to_neighbours(target)

        return processed_packets

    def change_link_cost(self, l: int, r: int, cost: int) -> None:
        if l not in self.routers or r not in self.routers[l].neighbours:
            raise ValueError(f"link {l}-{r} does not exist")

        self.routers[l].set_link_cost(r, cost)
        self.routers[r].set_link_cost(l, cost)
        for src in (l, r):
            self.routers[src].recalc()
            self.send_vector_to_neighbours(src)

    def print_tables(self, title: str) -> None:
        print(f"\n{title}")
        for router_id, router in sorted(self.routers.items()):
            print(f"router {router_id}")
            for node in sorted(self.routers):
                route = router.table.get(node)
                if route is None:
                    print(f"  to {node}: cost=inf next=-")
                    continue

                next_hop = "-" if route.next_hop is None else route.next_hop
                print(f"  to {node}: cost={route.cost} next={next_hop}")

    def assert_costs(self, expected_costs: dict[int, dict[int, int]]) -> None:
        for router_id, costs in expected_costs.items():
            for node, expected_cost in costs.items():
                route = self.routers[router_id].table.get(node)
                if route is None or route.cost != expected_cost:
                    actual_cost = "inf" if route is None else route.cost
                    raise AssertionError(
                        f"router {router_id} -> {node}: expected cost {expected_cost}, got {actual_cost}"
                    )


class ThreadedRouter(threading.Thread):
    def __init__(self, router: Router) -> None:
        super().__init__(daemon=True)
        self.router = router
        self.inbox: queue.Queue[tuple[int, dict[int, int]]] = queue.Queue()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.network: ThreadedDistanceVectorNetwork | None = None
        self.processed_packets = 0

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                src, vector = self.inbox.get(timeout=0.05)
            except queue.Empty:
                continue

            vector_to_send = None
            neighbours: list[int] = []
            try:
                with self.lock:
                    if self.router.apply_vector(src, vector):
                        vector_to_send = self.router.vector()
                        neighbours = sorted(self.router.neighbours)
                    self.processed_packets += 1

                if vector_to_send is not None and self.network is not None:
                    self.network.send_vector(
                        self.router.name, neighbours, vector_to_send
                    )
            finally:
                self.inbox.task_done()
                if self.network is not None:
                    self.network.packet_processed()


class ThreadedDistanceVectorNetwork:
    def __init__(self, links: tuple[tuple[int, int, int], ...]) -> None:
        nodes = sorted({node for link in links for node in link[:2]})
        self.routers = {node: ThreadedRouter(Router(node)) for node in nodes}
        self.condition = threading.Condition()
        self.sent_packets = 0
        self.pending_packets = 0

        for l, r, cost in links:
            self.routers[l].router.set_link_cost(r, cost)
            self.routers[r].router.set_link_cost(l, cost)

        for threaded_router in self.routers.values():
            threaded_router.router.recalc()
            threaded_router.network = self

    def start(self) -> None:
        for threaded_router in self.routers.values():
            threaded_router.start()

    def stop(self) -> None:
        for threaded_router in self.routers.values():
            threaded_router.stop_event.set()
        for threaded_router in self.routers.values():
            threaded_router.join(timeout=1.0)

    def send_vector(
        self, src: int, neighbours: list[int], vector: dict[int, int]
    ) -> None:
        with self.condition:
            self.sent_packets += len(neighbours)
            self.pending_packets += len(neighbours)
            for neighbour in neighbours:
                self.routers[neighbour].inbox.put((src, dict(vector)))
            self.condition.notify_all()

    def packet_processed(self) -> None:
        with self.condition:
            self.pending_packets -= 1
            if self.pending_packets < 0:
                raise RuntimeError("threaded packet accounting went below zero")
            self.condition.notify_all()

    def send_vector_to_neighbours(self, src: int) -> None:
        threaded_router = self.routers[src]
        with threaded_router.lock:
            vector = threaded_router.router.vector()
            neighbours = sorted(threaded_router.router.neighbours)

        self.send_vector(src, neighbours, vector)

    def send_all_vectors(self) -> None:
        for src in sorted(self.routers):
            self.send_vector_to_neighbours(src)

    def change_link_cost(self, l: int, r: int, cost: int) -> None:
        if l not in self.routers or r not in self.routers[l].router.neighbours:
            raise ValueError(f"link {l}-{r} does not exist")

        first, second = sorted((l, r))
        updates_to_send = []
        with self.routers[first].lock:
            with self.routers[second].lock:
                self.routers[l].router.set_link_cost(r, cost)
                self.routers[r].router.set_link_cost(l, cost)
                for src in (l, r):
                    self.routers[src].router.recalc()
                    updates_to_send.append(
                        (
                            src,
                            sorted(self.routers[src].router.neighbours),
                            self.routers[src].router.vector(),
                        )
                    )

        for src, neighbours, vector in updates_to_send:
            self.send_vector(src, neighbours, vector)

    def wait_until_all_packets_processed(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.pending_packets != 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)

            if self.pending_packets == 0:
                return

        raise TimeoutError(
            "threaded network did not process all packets before timeout"
        )

    def print_tables(self, title: str) -> None:
        print(f"\n{title}")
        for router_id, threaded_router in sorted(self.routers.items()):
            with threaded_router.lock:
                table = dict(threaded_router.router.table)
            print(f"router {router_id}")
            for node in sorted(self.routers):
                route = table.get(node)
                if route is None:
                    print(f"  to {node}: cost=inf next=-")
                    continue

                next_hop = "-" if route.next_hop is None else route.next_hop
                print(f"  to {node}: cost={route.cost} next={next_hop}")

    def assert_costs(self, expected_costs: dict[int, dict[int, int]]) -> None:
        for router_id, costs in expected_costs.items():
            with self.routers[router_id].lock:
                table = dict(self.routers[router_id].router.table)
            for node, expected_cost in costs.items():
                route = table.get(node)
                if route is None or route.cost != expected_cost:
                    actual_cost = "inf" if route is None else route.cost
                    raise AssertionError(
                        f"router {router_id} -> {node}: expected cost {expected_cost}, got {actual_cost}"
                    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("sync", "threaded", "all"), default="all")
    parser.add_argument("--thread-timeout", type=float, default=3.0)
    args = parser.parse_args()

    if args.thread_timeout <= 0:
        parser.error("--thread-timeout must be greater than 0")

    if args.mode in {"sync", "all"}:
        print("=== Synchronous distance-vector routing ===")
        network = DistanceVectorNetwork(BASE_LINKS)
        network.send_all_vectors()
        processed_packets = network.process_updates_until_stable()
        network.print_tables(
            f"Initial convergence, processed packets={processed_packets}"
        )
        network.assert_costs(EXPECTED_INITIAL_COSTS)

        print("\nChanging link 1-2 cost: 1 -> 6")
        network.change_link_cost(1, 2, 6)
        processed_packets = network.process_updates_until_stable()
        network.print_tables(
            f"After link 1-2 update, processed packets={processed_packets}"
        )
        network.assert_costs(EXPECTED_AFTER_1_2_INCREASE)

        print("\nChanging link 0-3 cost: 7 -> 2")
        network.change_link_cost(0, 3, 2)
        processed_packets = network.process_updates_until_stable()
        network.print_tables(
            f"After link 0-3 update, processed packets={processed_packets}"
        )
        network.assert_costs(EXPECTED_AFTER_0_3_DECREASE)
        print("Synchronous checks passed.")

    if args.mode in {"threaded", "all"}:
        if args.mode == "all":
            print()
        print("=== Threaded asynchronous distance-vector routing ===")
        threaded_network = ThreadedDistanceVectorNetwork(BASE_LINKS)
        threaded_network.start()
        try:
            threaded_network.send_all_vectors()
            threaded_network.wait_until_all_packets_processed(args.thread_timeout)
            threaded_network.print_tables("Initial threaded convergence")
            threaded_network.assert_costs(EXPECTED_INITIAL_COSTS)

            print("\nChanging link 1-2 cost: 1 -> 6")
            threaded_network.change_link_cost(1, 2, 6)
            threaded_network.wait_until_all_packets_processed(args.thread_timeout)
            threaded_network.print_tables("Threaded tables after link 1-2 update")
            threaded_network.assert_costs(EXPECTED_AFTER_1_2_INCREASE)

            print("\nChanging link 0-3 cost: 7 -> 2")
            threaded_network.change_link_cost(0, 3, 2)
            threaded_network.wait_until_all_packets_processed(args.thread_timeout)
            threaded_network.print_tables("Threaded tables after link 0-3 update")
            threaded_network.assert_costs(EXPECTED_AFTER_0_3_DECREASE)
            print("Threaded checks passed.")
        finally:
            threaded_network.stop()
