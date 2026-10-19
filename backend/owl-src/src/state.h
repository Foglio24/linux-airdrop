/*
 * OWL: an open Apple Wireless Direct Link (AWDL) implementation
 * Copyright (C) 2018  The Open Wireless Link Project (https://owlink.org)
 * Copyright (C) 2018  Milan Stute
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

#ifndef AWDL_STATE_H_
#define AWDL_STATE_H_

#include <stdint.h>
#include <stdbool.h>

#include "wire.h"
#include "frame.h"
#include "peers.h"
#include "sync.h"
#include "channel.h"

/* Upstream used -65 dBm judged frame by frame. On the MT7922 an iPhone a metre away still sends about 1 frame in
 * 10 below -65 dBm (measured), so that setting threw away sync updates; the default is now a permissive threshold
 * judged on a running average. -DOWL_UPSTREAM_RSSI restores the old values (see -I and -r at run time too). */
#ifdef OWL_UPSTREAM_RSSI
#define RSSI_THRESHOLD_DEFAULT -65
#else
#define RSSI_THRESHOLD_DEFAULT -80
#endif
#define RSSI_GRACE_DEFAULT      -5
#define UNICAST_GUARD_DEFAULT_TU 3

struct awdl_state; /* forward declaration for tlv_cb */

typedef void (*awdl_tlv_cb)(struct awdl_peer *, uint8_t, const struct buf *, struct awdl_state *, void *);

struct awdl_stats {
	uint64_t tx_action;
	uint64_t tx_data;
	uint64_t tx_data_unicast;
	uint64_t tx_data_multicast;
	uint64_t rx_action;
	uint64_t rx_data;
	uint64_t rx_unknown;
	uint64_t tx_data_failed;
	uint64_t tx_action_failed;
	/* --- owl-nexa: where unicast time goes (see docs/OWL-NEXA.md) --- */
	uint64_t rx_rssi_ignored;                       /* action frames refused by the RSSI filter */
	uint64_t gate_sent;                             /* unicast frames handed to the radio */
	uint64_t gate_wait_channel;                     /* held: the peer is not on our channel in this slot */
	uint64_t gate_wait_end;                         /* held: too close to the end of a slot */
	uint64_t gate_wait_start;                       /* held: too close to the start of a slot */
	uint64_t gate_queue_full;                       /* radio queue full, retried */
	uint64_t gate_non_peer;                         /* dropped: destination is not a known peer */
	uint64_t held_us;                               /* total time frames waited for the gate */
	uint64_t tx_slot[16];                           /* unicast frames sent, by position in the 16-slot cycle */
};

/* How the receive filter judges a peer's signal. */
enum awdl_rssi_mode {
	AWDL_RSSI_AVERAGE = 0, /* a known peer is judged by a running average of its frames (default) */
	AWDL_RSSI_INSTANT = 1, /* upstream: every frame is judged on its own reading */
};

/* Guards kept at the edges of a shared slot before a unicast frame may leave (TU, 1 TU = 1024 us). */
struct awdl_tx_tuning {
	int start_guard_tu;
	int end_guard_tu;
};

/* Complete node state */
struct awdl_state {
	struct ether_addr self_address;
	char name[HOST_NAME_LENGTH_MAX + 1];

	uint8_t version;
	uint8_t dev_class;

	/* sequence number for data frames */
	uint16_t sequence_number;
	/* PSF interval (in TU) */
	uint16_t psf_interval;
	/* destination address for action frames */
	struct ether_addr dst;

	/* Allows to hook TLV reception */
	awdl_tlv_cb tlv_cb;
	void *tlv_cb_data;

	/* Allows to hook adding of new neighbor */
	awdl_peer_cb peer_cb;
	void *peer_cb_data;

	/* Allows to hook removing of new neighbor */
	awdl_peer_cb peer_remove_cb;
	void *peer_remove_cb_data;

	int filter_rssi; /* whether or not to filter (default: true) */
	signed char rssi_threshold; /* peers exceeding this threshold are discovered */
	signed char rssi_grace; /* once discovered accept lower RSSI */
	enum awdl_rssi_mode rssi_mode;
	struct awdl_tx_tuning tx;

	struct awdl_election_state election;
	struct awdl_sync_state sync;
	struct awdl_channel_state channel;
	struct awdl_peer_state peers;
	struct awdl_stats stats;
};

void awdl_stats_init(struct awdl_stats *stats);

void awdl_init_state(struct awdl_state *state, const char *hostname, const struct ether_addr *self,
                     struct awdl_chan chan, uint64_t now);

uint16_t awdl_state_next_sequence_number(struct awdl_state *);

struct ieee80211_state {
	/* IEEE 802.11 sequence number */
	uint16_t sequence_number;
	/* whether we need to add an fcs */
	bool fcs;
};

void ieee80211_init_state(struct ieee80211_state *);

unsigned int ieee80211_state_next_sequence_number(struct ieee80211_state *);

uint64_t clock_time_us();

#endif /* AWDL_STATE_H_ */
