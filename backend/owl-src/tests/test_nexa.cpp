/*
 * owl-nexa additions: the RSSI filter judged on a running average, separate start/end guards on the unicast
 * gate with the reason it is shut, and the statistics that say where unicast time goes.
 *
 * Pure logic: no radio, no clock. Numbers in the comments are from the captures of 18 October 2026
 * (MT7922, an iPhone about a metre away): median -52 dBm, one action frame in ten below -65 dBm, the iPhone
 * transmitting anywhere in a 64 TU slot (1st percentile 0.75 TU, 99th 62.7 TU).
 */

extern "C" {
#include "state.h"
#include "schedule.h"
#include "peers.h"
#include "channel.h"
#include "rx.h"
#include "wire.h"
#include "ieee80211.h"
}

#include "gtest/gtest.h"
#include <cstring>

static const uint64_t EAW_USEC = 1024ULL * 64;

/* A valid PSF with no TLVs: the 16-byte AWDL action header (vendor category, OUI, type, version, subtype). */
static int rx_psf(struct awdl_state *state, const struct ether_addr *src, signed char rssi) {
	static const uint8_t psf[] = {127, 0x00, 0x17, 0xf2, 8, 0x10, 0 /* PSF */, 0, 0, 0, 0, 0, 0, 0, 0, 0};
	const struct buf *frame = buf_new_const(psf, sizeof(psf));
	struct ether_addr dst = {{0xff, 0xff, 0xff, 0xff, 0xff, 0xff}};
	int result = awdl_rx_action(frame, rssi, 1000, src, &dst, state);
	buf_free(frame);
	return result;
}

class Rssi : public ::testing::Test {
  protected:
	struct awdl_state state;
	struct ether_addr self = {{0x4c, 0x82, 0xa9, 0x17, 0x65, 0x27}};
	struct ether_addr phone = {{0x32, 0x57, 0x70, 0x60, 0x8d, 0xec}};

	void SetUp() override {
		struct awdl_chan c44 = CHAN_OPCLASS_44;
		awdl_init_state(&state, "nexa", &self, c44, 0);
	}
};

TEST_F(Rssi, defaults_are_a_permissive_threshold_judged_on_an_average) {
	EXPECT_EQ(state.rssi_threshold, -80);
	EXPECT_EQ(state.rssi_mode, AWDL_RSSI_AVERAGE);
	EXPECT_EQ(state.tx.start_guard_tu, 3);
	EXPECT_EQ(state.tx.end_guard_tu, 3);
}

TEST_F(Rssi, a_new_peer_at_minus_70_is_discovered_where_upstream_refused_it) {
	EXPECT_EQ(rx_psf(&state, &phone, -70), RX_OK);
	EXPECT_EQ(state.stats.rx_rssi_ignored, 0u);
	EXPECT_EQ(awdl_peers_length(state.peers.peers), 1);
}

TEST_F(Rssi, a_new_peer_below_the_threshold_is_still_refused_and_counted) {
	EXPECT_EQ(rx_psf(&state, &phone, -85), RX_IGNORE_RSSI);
	EXPECT_EQ(state.stats.rx_rssi_ignored, 1u);
	EXPECT_EQ(awdl_peers_length(state.peers.peers), 0);
}

TEST_F(Rssi, one_deep_fade_of_a_strong_known_peer_does_not_cost_the_frame) {
	for (int i = 0; i < 20; i++)
		ASSERT_EQ(rx_psf(&state, &phone, -52), RX_OK);
	/* the measured case: a frame at -94 dBm from a peer whose median is -52 */
	EXPECT_EQ(rx_psf(&state, &phone, -94), RX_OK);
	EXPECT_EQ(state.stats.rx_rssi_ignored, 0u);
}

TEST_F(Rssi, upstream_mode_judges_each_frame_alone) {
	state.rssi_mode = AWDL_RSSI_INSTANT;
	state.rssi_threshold = -65;
	for (int i = 0; i < 20; i++)
		ASSERT_EQ(rx_psf(&state, &phone, -52), RX_OK);
	EXPECT_EQ(rx_psf(&state, &phone, -75), RX_IGNORE_RSSI); /* below -65 + grace -5 */
	EXPECT_EQ(state.stats.rx_rssi_ignored, 1u);
}

TEST_F(Rssi, a_peer_that_really_goes_away_is_refused_after_a_few_frames) {
	for (int i = 0; i < 20; i++)
		ASSERT_EQ(rx_psf(&state, &phone, -52), RX_OK);
	int accepted = 0;
	for (int i = 0; i < 200; i++) {
		if (rx_psf(&state, &phone, -95) == RX_OK)
			accepted++;
		else
			break;
	}
	EXPECT_GT(accepted, 0);
	EXPECT_LT(accepted, 40) << "the average must follow a peer that has left, not hold on to it";
}

TEST_F(Rssi, the_filter_can_be_switched_off) {
	state.filter_rssi = 0;
	EXPECT_EQ(rx_psf(&state, &phone, -99), RX_OK);
	EXPECT_EQ(state.stats.rx_rssi_ignored, 0u);
}

TEST_F(Rssi, the_average_does_not_overflow_on_extreme_readings) {
	EXPECT_EQ(rx_psf(&state, &phone, -60), RX_OK);
	for (int i = 0; i < 1000; i++)
		rx_psf(&state, &phone, (i & 1) ? -127 : 126);
	SUCCEED();
}

/* ------------------------------------------------------------------------------------------------ the gate */

/* An idle iPhone on a busy 5 GHz channel: slots 0, 1 and 9..11 on 44 (three consecutive slots shared) and slot 4 alone. */
static const uint8_t PHONE_44[AWDL_CHANSEQ_LENGTH] = {44, 44, 0, 0, 44, 0, 0, 0, 6, 44, 44, 44, 0, 0, 0, 0};

class Gate : public ::testing::Test {
  protected:
	struct awdl_state state;
	struct awdl_peer *peer;

	void SetUp() override {
		struct ether_addr self = {{0x4c, 0x82, 0xa9, 0x17, 0x65, 0x27}};
		struct ether_addr paddr = {{0x32, 0x57, 0x70, 0x60, 0x8d, 0xec}};
		struct awdl_chan c44 = CHAN_OPCLASS_44;
		awdl_init_state(&state, "nexa", &self, c44, 0);
		state.sync.last_update = 0;
		state.sync.aw_counter = 0;
		state.channel.fixed_radio = 1;
		awdl_peer_add(state.peers.peers, &paddr, 0, NULL, NULL);
		ASSERT_EQ(awdl_peer_get(state.peers.peers, &paddr, &peer), PEERS_OK);
		for (int i = 0; i < AWDL_CHANSEQ_LENGTH; i++) {
			peer->sequence[i].opclass.chan_num = PHONE_44[i];
			peer->sequence[i].opclass.opclass = PHONE_44[i] ? (PHONE_44[i] > 14 ? 0x80 : 0x51) : 0;
		}
		peer->sequence_enc = AWDL_CHAN_ENC_OPCLASS;
		peer->has_sequence = 1;
	}

	/* a moment `tu` TU into slot `slot` */
	uint64_t at(int slot, double tu) { return (uint64_t) slot * EAW_USEC + (uint64_t) (tu * 1024); }
};

TEST_F(Gate, defaults_behave_exactly_like_the_symmetric_upstream_guard) {
	for (int slot = 0; slot < 16; slot++)
		for (double tu : {0.2, 1.0, 2.9, 3.1, 30.0, 60.9, 61.1, 63.5}) {
			enum awdl_gate_reason reason;
			uint64_t now = at(slot, tu);
			EXPECT_DOUBLE_EQ(awdl_unicast_gate(&state, peer, now, &reason),
			                 awdl_can_send_unicast_in(&state, peer, now, 3))
			    << "slot " << slot << " at " << tu << " TU";
		}
}

TEST_F(Gate, reasons_say_why_a_frame_waits) {
	enum awdl_gate_reason reason;
	EXPECT_EQ(awdl_unicast_gate(&state, peer, at(2, 30), &reason) > 0, true); /* slot 2: the phone is elsewhere */
	EXPECT_EQ(reason, AWDL_GATE_WAIT_CHANNEL);
	EXPECT_DOUBLE_EQ(awdl_unicast_gate(&state, peer, at(4, 30), &reason), 0.0); /* slot 4 alone, mid-slot */
	EXPECT_EQ(reason, AWDL_GATE_OPEN);
	EXPECT_LT(awdl_unicast_gate(&state, peer, at(4, 62), &reason), 0.0);      /* last 2 TU of an isolated slot */
	EXPECT_EQ(reason, AWDL_GATE_WAIT_END);
	EXPECT_GT(awdl_unicast_gate(&state, peer, at(4, 1), &reason), 0.0);       /* first TU of an isolated slot */
	EXPECT_EQ(reason, AWDL_GATE_WAIT_START);
}

TEST_F(Gate, shared_neighbouring_slots_ignore_the_guard_at_the_shared_edge) {
	enum awdl_gate_reason reason;
	/* slots 9, 10, 11 are all on 44: slot 9's end and slot 10's start are shared, no guard there */
	EXPECT_DOUBLE_EQ(awdl_unicast_gate(&state, peer, at(9, 63), &reason), 0.0);
	EXPECT_DOUBLE_EQ(awdl_unicast_gate(&state, peer, at(10, 0.5), &reason), 0.0);
	/* but slot 9's start (slot 8 is channel 6) is not shared */
	EXPECT_GT(awdl_unicast_gate(&state, peer, at(9, 1), &reason), 0.0);
	EXPECT_EQ(reason, AWDL_GATE_WAIT_START);
}

TEST_F(Gate, smaller_guards_open_the_edges_of_an_isolated_slot) {
	enum awdl_gate_reason reason;
	state.tx.start_guard_tu = 1;
	state.tx.end_guard_tu = 1;
	EXPECT_DOUBLE_EQ(awdl_unicast_gate(&state, peer, at(4, 62), &reason), 0.0); /* was shut with the 3 TU guard */
	EXPECT_DOUBLE_EQ(awdl_unicast_gate(&state, peer, at(4, 1.5), &reason), 0.0);
	EXPECT_LT(awdl_unicast_gate(&state, peer, at(4, 63.5), &reason), 0.0);      /* inside the last TU: still shut */
}

TEST_F(Gate, start_and_end_guards_are_independent) {
	enum awdl_gate_reason reason;
	state.tx.start_guard_tu = 0;
	state.tx.end_guard_tu = 5;
	EXPECT_DOUBLE_EQ(awdl_unicast_gate(&state, peer, at(4, 0.1), &reason), 0.0);
	EXPECT_LT(awdl_unicast_gate(&state, peer, at(4, 60), &reason), 0.0);
	EXPECT_EQ(reason, AWDL_GATE_WAIT_END);
}

TEST_F(Gate, zero_guards_never_close_a_shared_slot) {
	enum awdl_gate_reason reason;
	state.tx.start_guard_tu = 0;
	state.tx.end_guard_tu = 0;
	for (double tu = 0; tu < 64; tu += 0.25)
		EXPECT_DOUBLE_EQ(awdl_unicast_gate(&state, peer, at(4, tu), &reason), 0.0) << tu;
}

TEST(Stats, start_at_zero) {
	struct awdl_stats stats;
	memset(&stats, 0xff, sizeof(stats));
	awdl_stats_init(&stats);
	EXPECT_EQ(stats.gate_sent, 0u);
	EXPECT_EQ(stats.held_us, 0u);
	EXPECT_EQ(stats.rx_rssi_ignored, 0u);
	for (int i = 0; i < 16; i++)
		EXPECT_EQ(stats.tx_slot[i], 0u);
}
