/* Regression for the recorded October 2026 disagreement: a new master with
 * metric 522/counter393 must beat metric510/counter3214 only with -E. */
#include <string.h>
#include "election.h"
#include "peers.h"

static const struct ether_addr self = {{2, 0, 0, 0, 0, 1}};
static const struct ether_addr other = {{2, 0, 0, 0, 0, 2}};

int main(void) {
    struct awdl_election_state state;
    struct awdl_peer_state peers;
    struct awdl_peer *peer;
    awdl_peer_state_init(&peers);
    awdl_peer_add(peers.peers, &other, 0, NULL, NULL);
    awdl_peer_get(peers.peers, &other, &peer);
    peer->is_valid = 1;
    peer->election.master_addr = other;
    peer->election.self_addr = other;
    peer->election.master_metric = 522;
    peer->election.master_counter = 393;
    for (int enabled = 0; enabled < 2; enabled++) {
        awdl_election_state_init(&state, &self);
        state.self_metric = 510;
        state.self_counter = 3214;
        state.metric_first = enabled;
        awdl_election_run(&state, &peers);
        const struct ether_addr *expected = enabled ? &other : &self;
        if (memcmp(&state.master_addr, expected, sizeof(*expected)))
            return 1;
    }
    awdl_peers_free(peers.peers);
    return 0;
}
