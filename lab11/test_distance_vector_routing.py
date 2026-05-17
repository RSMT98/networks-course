import unittest

from distance_vector_routing import (
    BASE_LINKS,
    EXPECTED_AFTER_0_3_DECREASE,
    EXPECTED_AFTER_1_2_INCREASE,
    EXPECTED_INITIAL_COSTS,
    DistanceVectorNetwork,
    ThreadedDistanceVectorNetwork,
)

EXPECTED_INITIAL_NEXT_HOPS = {
    0: {0: None, 1: 1, 2: 1, 3: 1},
    1: {0: 0, 1: None, 2: 2, 3: 2},
    2: {0: 1, 1: 1, 2: None, 3: 3},
    3: {0: 2, 1: 2, 2: 2, 3: None},
}

EXPECTED_AFTER_1_2_INCREASE_NEXT_HOPS = {
    0: {0: None, 1: 1, 2: 2, 3: 2},
    1: {0: 0, 1: None, 2: 0, 3: 0},
    2: {0: 0, 1: 0, 2: None, 3: 3},
    3: {0: 2, 1: 2, 2: 2, 3: None},
}

EXPECTED_AFTER_0_3_DECREASE_NEXT_HOPS = {
    0: {0: None, 1: 1, 2: 2, 3: 3},
    1: {0: 0, 1: None, 2: 0, 3: 0},
    2: {0: 0, 1: 0, 2: None, 3: 3},
    3: {0: 0, 1: 0, 2: 2, 3: None},
}


class NextHopAssertions:
    def assert_next_hops(self, tables: dict, expected_next_hops: dict) -> None:
        for router_id, next_hops in expected_next_hops.items():
            for node, expected_next_hop in next_hops.items():
                self.assertEqual(
                    expected_next_hop,
                    tables[router_id][node].next_hop,
                    f"router {router_id} -> {node}",
                )


class DistanceVectorNetworkTest(NextHopAssertions, unittest.TestCase):
    def test_sync_network_updates_routes_after_link_cost_changes(self) -> None:
        network = DistanceVectorNetwork(BASE_LINKS)
        network.send_all_vectors()
        network.process_updates_until_stable()
        network.assert_costs(EXPECTED_INITIAL_COSTS)
        self.assert_next_hops(
            {router_id: router.table for router_id, router in network.routers.items()},
            EXPECTED_INITIAL_NEXT_HOPS,
        )

        network.change_link_cost(1, 2, 6)
        network.process_updates_until_stable()
        network.assert_costs(EXPECTED_AFTER_1_2_INCREASE)
        self.assert_next_hops(
            {router_id: router.table for router_id, router in network.routers.items()},
            EXPECTED_AFTER_1_2_INCREASE_NEXT_HOPS,
        )

        network.change_link_cost(0, 3, 2)
        network.process_updates_until_stable()
        network.assert_costs(EXPECTED_AFTER_0_3_DECREASE)
        self.assert_next_hops(
            {router_id: router.table for router_id, router in network.routers.items()},
            EXPECTED_AFTER_0_3_DECREASE_NEXT_HOPS,
        )


class ThreadedDistanceVectorNetworkTest(NextHopAssertions, unittest.TestCase):
    def test_threaded_network_updates_routes_after_link_cost_changes(self) -> None:
        network = ThreadedDistanceVectorNetwork(BASE_LINKS)
        network.start()
        self.addCleanup(network.stop)

        network.send_all_vectors()
        network.wait_until_all_packets_processed(3.0)
        network.assert_costs(EXPECTED_INITIAL_COSTS)
        tables = {}
        for router_id, threaded_router in network.routers.items():
            with threaded_router.lock:
                tables[router_id] = dict(threaded_router.router.table)
        self.assert_next_hops(tables, EXPECTED_INITIAL_NEXT_HOPS)

        network.change_link_cost(1, 2, 6)
        network.wait_until_all_packets_processed(3.0)
        network.assert_costs(EXPECTED_AFTER_1_2_INCREASE)
        tables = {}
        for router_id, threaded_router in network.routers.items():
            with threaded_router.lock:
                tables[router_id] = dict(threaded_router.router.table)
        self.assert_next_hops(tables, EXPECTED_AFTER_1_2_INCREASE_NEXT_HOPS)
        with network.routers[1].lock:
            with network.routers[2].lock:
                self.assertEqual(6, network.routers[1].router.neighbours[2])
                self.assertEqual(6, network.routers[2].router.neighbours[1])

        network.change_link_cost(0, 3, 2)
        network.wait_until_all_packets_processed(3.0)
        network.assert_costs(EXPECTED_AFTER_0_3_DECREASE)
        tables = {}
        for router_id, threaded_router in network.routers.items():
            with threaded_router.lock:
                tables[router_id] = dict(threaded_router.router.table)
        self.assert_next_hops(tables, EXPECTED_AFTER_0_3_DECREASE_NEXT_HOPS)
        with network.condition:
            self.assertEqual(0, network.pending_packets)


if __name__ == "__main__":
    unittest.main()
