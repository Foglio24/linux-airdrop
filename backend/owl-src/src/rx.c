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

#include <string.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/ip6.h>
#include <radiotap.h>
#include <radiotap_iter.h>

#include "rx.h"
#include "sync.h"
#include "wire.h"
#include "log.h"

#define AWDL_SYNC_THRESHOLD 3

int awdl_handle_sync_params_tlv(struct awdl_peer *src, const struct buf *val, struct awdl_state *state, uint64_t now) {
	uint16_t aw_counter_master;
	uint16_t time_to_next_aw_master;
	int64_t sync_err_tu;

	/* Read the announced phase for EVERY peer, not just the master. Our own
	 * clock only ever tracks the master (below), but knowing how far each other
	 * peer's availability windows sit from ours is what makes it possible to
	 * reason about a non-master's channel sequence at all -- see
	 * awdl_same_channel_as_peer() and the chanseq rotation in core.c, both of
	 * which read sync_offset and, until now, always got 0. */
	READ_LE16(val, 1, &time_to_next_aw_master);
	READ_LE16(val, 29, &aw_counter_master);

	/* awdl_sync_error_tu() is just "peer's announced phase minus ours", which is
	 * exactly the correction we want; it is not master-specific. Converted with
	 * an explicit signed multiply rather than ieee80211_tu_to_usec(), which
	 * takes unsigned long and would round a negative offset through wraparound. */
	src->sync_offset = (int64_t) 1024 *
	                   awdl_sync_error_tu(now, time_to_next_aw_master, aw_counter_master, &state->sync);
	src->has_sync_offset = 1;

	if (!awdl_election_is_sync_master(&state->election, &src->addr))
		return RX_IGNORE; /* ignore sync params from nodes that are not our master */

	/* TODO synchronization could be more accurate */

	state->sync.meas_total++;
	sync_err_tu = awdl_sync_error_tu(now, time_to_next_aw_master, aw_counter_master, &state->sync);
	if (sync_err_tu > AWDL_SYNC_THRESHOLD || sync_err_tu < -AWDL_SYNC_THRESHOLD) {
		state->sync.meas_err++;
		log_trace("Sync error %d TU (%.02f %%)", sync_err_tu, state->sync.meas_err * 100.0 / state->sync.meas_total);
	}
	awdl_sync_update_last(now, time_to_next_aw_master, aw_counter_master, &state->sync);

	return RX_OK;
wire_error:
	return RX_TOO_SHORT;
}

static int awdl_parse_chanseq(const struct buf *val, const struct awdl_state *state,
                              uint8_t *sequence_encoding, struct awdl_chan *list) {
	uint8_t count;
	uint8_t encoding;
	uint8_t duplicate_count;
	uint8_t step_count;
	uint16_t fill_channel;
	int size;

	/* read and sanity check, we do not support any other configuration */
	READ_U8(val, 0, &count);
	if (count + 1 != AWDL_CHANSEQ_LENGTH)
		return RX_UNEXPECTED_VALUE;
	READ_U8(val, 2, &duplicate_count);
	if (duplicate_count > 0)
		return RX_UNEXPECTED_VALUE;
	READ_U8(val, 3, &step_count);
	if (step_count + 1 != state->sync.presence_mode)
		return RX_UNEXPECTED_VALUE;
	READ_LE16(val, 4, &fill_channel);
	if (fill_channel != 0xffff)
		return RX_UNEXPECTED_VALUE;

	READ_U8(val, 1, &encoding);
	size = awdl_chan_encoding_size(encoding);
	if (size < 1)
		return RX_UNEXPECTED_VALUE;

	for (int i = 0, offset = 6; i < AWDL_CHANSEQ_LENGTH; i++, offset += size)
		READ_BYTES_COPY(val, offset, list ? list[i].val : NULL, size);

	if (sequence_encoding)
		*sequence_encoding = encoding;
	return RX_OK;
wire_error:
	return RX_TOO_SHORT;
}

int awdl_handle_chanseq_tlv(struct awdl_peer *src, const struct buf *val, struct awdl_state *state) {
	uint8_t encoding;
	struct awdl_chan list[AWDL_CHANSEQ_LENGTH] = {0};
	int result = awdl_parse_chanseq(val, state, &encoding, list);
	if (result < 0)
		return result;

	if (memcmp(src->sequence, list, sizeof(list))) {
		log_debug("peer %s (%s) changed channel sequence to %d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d",
		          ether_ntoa(&src->addr), src->name,
		          awdl_chan_num(list[0], encoding), awdl_chan_num(list[1], encoding),
		          awdl_chan_num(list[2], encoding), awdl_chan_num(list[3], encoding),
		          awdl_chan_num(list[4], encoding), awdl_chan_num(list[5], encoding),
		          awdl_chan_num(list[6], encoding), awdl_chan_num(list[7], encoding),
		          awdl_chan_num(list[8], encoding), awdl_chan_num(list[9], encoding),
		          awdl_chan_num(list[10], encoding), awdl_chan_num(list[11], encoding),
		          awdl_chan_num(list[12], encoding), awdl_chan_num(list[13], encoding),
		          awdl_chan_num(list[14], encoding), awdl_chan_num(list[15], encoding));
		memcpy(src->sequence, list, sizeof(list));
	}
	src->sequence_enc = encoding;
	src->has_sequence = 1;
	return RX_OK;

}

int awdl_handle_election_params_tlv(struct awdl_peer *src, const struct buf *val,
                                    struct awdl_state *state __attribute__((unused))) {
	uint8_t distance_to_master;
	struct awdl_election_state election = src->election;

	if (src->supports_v2)
		return 0; /* if node supports election v2, we ignore v1 advertisements */

	// READ_U8(val, 0, 0); /* ignore flags */
	// READ_LE16(val, 1, 0); /* TODO ignore ID */
	READ_U8(val, 3, &distance_to_master);
	election.height = distance_to_master;
	// READ_U8(val, 4, 0); /* ignore unknown */
	READ_ETHER_ADDR(val, 5, &election.master_addr);
	READ_LE32(val, 11, &election.master_metric);
	READ_LE32(val, 15, &election.self_metric);
	// READ_LE16(val, 19, 0); /* padding */

	src->election = election;
	return RX_OK;
wire_error:
	return RX_TOO_SHORT;
}

int awdl_handle_election_params_v2_tlv(struct awdl_peer *src, const struct buf *val,
                                       struct awdl_state *state __attribute__((unused))) {
	struct awdl_election_state election = src->election;
	READ_LE32(val, 16, &election.height);
	READ_ETHER_ADDR(val, 0, &election.master_addr);
	READ_ETHER_ADDR(val, 6, &election.sync_addr);
	READ_LE32(val, 12, &election.master_counter);
	READ_LE32(val, 20, &election.master_metric);
	READ_LE32(val, 24, &election.self_metric);
	// READ_LE32(val, 28, &tlv->unknown); /* ignore unknown */
	// READ_LE32(val, 32, &tlv->reserved); /* ignore reserved */
	READ_LE32(val, 36, &election.self_counter);

	src->election = election;
	src->supports_v2 = 1;
	return RX_OK;
wire_error:
	return RX_TOO_SHORT;
}

int awdl_handle_arpa_tlv(struct awdl_peer *src, const struct buf *val,
                         struct awdl_state *state __attribute__((unused))) {
	// READ_U8(val, 0, &flags); /* semantics unclear, ignore */
	READ_INT_STRING(val, 1, src->name, HOST_NAME_LENGTH_MAX);
	return RX_OK;
wire_error:
	return RX_TOO_SHORT;
}

int awdl_handle_data_path_state_tlv(struct awdl_peer *src, const struct buf *val,
                                    struct awdl_state *state __attribute__((unused))) {
	uint16_t flags;
	char country_code[sizeof(src->country_code)];
	struct ether_addr infra_addr;
	int offset = 0;
	READ_LE16(val, offset, &flags);
	offset += 2;

	if (flags & AWDL_DATA_PATH_FLAG_COUNTRY_CODE) {
		READ_BYTES_COPY(val, offset, (uint8_t *) country_code, 3);
		offset += 3;
	}
	if (flags & AWDL_DATA_PATH_FLAG_SOCIAL_CHANNEL_MAP) {
		READ_LE16(val, offset, NULL /* supported social channels */);
		offset += 2;
	}
	if (flags & AWDL_DATA_PATH_FLAG_INFRA_INFO) {
		READ_ETHER_ADDR(val, offset, NULL /* BSSID */);
		offset += 6;
		READ_LE16(val, offset, NULL /* channel */);
		offset += 2;
	}
	if (flags & AWDL_DATA_PATH_FLAG_INFRA_ADDRESS) {
		READ_ETHER_ADDR(val, offset, &infra_addr);
		offset += 6;
	}
	if (flags & AWDL_DATA_PATH_FLAG_AWDL_ADDRESS) {
		READ_ETHER_ADDR(val, offset, NULL /* AWDL address */);
		offset += 6;
	}

	if (flags & AWDL_DATA_PATH_FLAG_COUNTRY_CODE)
		memcpy(src->country_code, country_code, sizeof(country_code));
	if (flags & AWDL_DATA_PATH_FLAG_INFRA_ADDRESS)
		src->infra_addr = infra_addr;

	/* TODO complete parsing */
	return RX_OK;
wire_error:
	return RX_TOO_SHORT;
}

int awdl_handle_version_tlv(struct awdl_peer *src, const struct buf *val,
                            struct awdl_state *state __attribute__((unused))) {
	uint8_t version, devclass;
	READ_U8(val, 0, &version);
	READ_U8(val, 1, &devclass);
	src->version = version;
	src->devclass = devclass;
	return RX_OK;
wire_error:
	return RX_TOO_SHORT;
}

/* Check each supported value before any peer, clock or callback can observe it.
 * Unknown TLVs remain forward compatible, provided their envelope is complete. */
static int awdl_validate_tlv(uint8_t type, const struct buf *val, const struct awdl_state *state) {
	int required;
	uint16_t flags;
	switch (type) {
		case AWDL_SYNCHRONIZATON_PARAMETERS_TLV:
			required = 31;
			break;
		case AWDL_CHAN_SEQ_TLV:
			return awdl_parse_chanseq(val, state, NULL, NULL);
		case AWDL_ELECTION_PARAMETERS_TLV:
			required = 19;
			break;
		case AWDL_ELECTION_PARAMETERS_V2_TLV:
			required = 40;
			break;
		case AWDL_ARPA_TLV:
			return read_int_string(val, 1, NULL, HOST_NAME_LENGTH_MAX) < 0 ? RX_TOO_SHORT : RX_OK;
		case AWDL_DATA_PATH_STATE_TLV:
			if (read_le16(val, 0, &flags) < 0)
				return RX_TOO_SHORT;
			required = 2;
			if (flags & AWDL_DATA_PATH_FLAG_COUNTRY_CODE)
				required += 3;
			if (flags & AWDL_DATA_PATH_FLAG_SOCIAL_CHANNEL_MAP)
				required += 2;
			if (flags & AWDL_DATA_PATH_FLAG_INFRA_INFO)
				required += 8;
			if (flags & AWDL_DATA_PATH_FLAG_INFRA_ADDRESS)
				required += ETHER_ADDR_LEN;
			if (flags & AWDL_DATA_PATH_FLAG_AWDL_ADDRESS)
				required += ETHER_ADDR_LEN;
			break;
		case AWDL_VERSION_TLV:
			required = 2;
			break;
		default:
			return RX_IGNORE;
	}
	return read_bytes(val, 0, NULL, required) < 0 ? RX_TOO_SHORT : RX_OK;
}

static int awdl_validate_action_tlvs(const struct buf *frame, const struct awdl_state *state) {
	int offset = 0;
	while (offset < buf_len(frame)) {
		uint8_t type;
		uint16_t length;
		const uint8_t *value;
		const struct buf *val;
		int result, consumed;
		/* iPhones may leave one or two zero bytes after the final TLV. */
		if (buf_len(frame) - offset < 3) {
			while (offset < buf_len(frame))
				if (buf_data(frame)[offset++] != 0)
					return RX_UNEXPECTED_FORMAT;
			return RX_OK;
		}
		consumed = read_tlv(frame, offset, &type, &length, &value);
		if (consumed < 0)
			return RX_UNEXPECTED_FORMAT;
		val = buf_new_const(value, length);
		if (!val)
			return RX_NO_MEMORY;
		result = awdl_validate_tlv(type, val, state);
		buf_free(val);
		if (result < 0)
			return RX_UNEXPECTED_FORMAT;
		offset += consumed;
	}
	return RX_OK;
}

int awdl_handle_tlv(struct awdl_peer *src, uint8_t type, const struct buf *val,
                    struct awdl_state *state, uint64_t tsft) {
	int result = awdl_validate_tlv(type, val, state);
	if (result < 0)
		return result;
	switch (type) {
		case AWDL_SYNCHRONIZATON_PARAMETERS_TLV:
			return awdl_handle_sync_params_tlv(src, val, state, tsft);
		case AWDL_CHAN_SEQ_TLV:
			return awdl_handle_chanseq_tlv(src, val, state);
		case AWDL_ELECTION_PARAMETERS_TLV:
			return awdl_handle_election_params_tlv(src, val, state);
		case AWDL_ELECTION_PARAMETERS_V2_TLV:
			return awdl_handle_election_params_v2_tlv(src, val, state);
		case AWDL_ARPA_TLV:
			return awdl_handle_arpa_tlv(src, val, state);
		case AWDL_DATA_PATH_STATE_TLV:
			return awdl_handle_data_path_state_tlv(src, val, state);
		case AWDL_VERSION_TLV:
			return awdl_handle_version_tlv(src, val, state);
		case AWDL_SYNCTREE_TLV: /* seems to be buggy, not used in our election process */
			/* fall through */
		default:
			log_trace("awdl: not handling %s (%u)", awdl_tlv_as_str(type), type);
			return RX_IGNORE;
	}
}

int awdl_parse_action_hdr(const struct buf *frame) {
	const struct awdl_action *af;
	int valid;

	READ_BYTES(frame, 0, NULL, sizeof(struct awdl_action));

	af = (const struct awdl_action *) buf_data(frame);

	valid = af->category == IEEE80211_VENDOR_SPECIFIC &&
	        !memcmp(&af->oui, &AWDL_OUI, sizeof(AWDL_OUI)) &&
	        af->type == AWDL_TYPE &&
	        af->version == AWDL_VERSION_COMPAT;

	if (valid && (af->subtype == AWDL_ACTION_PSF || af->subtype == AWDL_ACTION_MIF))
		return af->subtype;

wire_error:
	return -1;
}

int awdl_rx_action(const struct buf *frame, signed char rssi, uint64_t tsft,
                   const struct ether_addr *src, const struct ether_addr *dst,
                   struct awdl_state *state) {
	int len;
	enum peers_status status;
	uint8_t tlv_type;
	uint16_t tlv_len;
	const uint8_t *tlv_value;
	struct awdl_peer *peer;
	int subtype, validation;

	(void) dst; /* TODO ignore destination address for now, could be used to mitigate desynchronization attack */

	subtype = awdl_parse_action_hdr(frame);
	if (subtype < 0) {
		log_trace("awdl_action: not an action frame"); /* could be block ACK */
		return RX_IGNORE;
	}
	buf_strip(frame, sizeof(struct awdl_action));
	validation = awdl_validate_action_tlvs(frame, state);
	if (validation < 0)
		return validation;

	if (state->filter_rssi) {
		struct awdl_peer *known = NULL;
		status = awdl_peer_get(state->peers.peers, src, &known);
		if (status == PEERS_OK && known && state->rssi_mode == AWDL_RSSI_AVERAGE) {
			/* owl-nexa: a known peer is judged by the running average of its frames (weight 1/8), so one deep
			 * fade neither drops the frame nor the sync information inside it. */
			known->rssi_avg8 = known->rssi_known ? known->rssi_avg8 + ((int32_t) rssi * 8 - known->rssi_avg8) / 8
			                                     : (int32_t) rssi * 8;
			known->rssi_known = 1;
			if (known->rssi_avg8 < ((int32_t) state->rssi_threshold + state->rssi_grace) * 8) {
				state->stats.rx_rssi_ignored++;
				return RX_IGNORE_RSSI;
			}
		} else if (((status == PEERS_OK) && rssi < state->rssi_threshold + state->rssi_grace) ||
		           ((status == PEERS_MISSING) && rssi < state->rssi_threshold)) {
			state->stats.rx_rssi_ignored++;
			return RX_IGNORE_RSSI;
		}
	}
	state->stats.rx_action++;

	/* Update peer table */
	status = awdl_peer_add(state->peers.peers, src, tsft, state->peer_cb, state->peer_cb_data);
	if (status < 0) {
		log_warn("awdl_action: could not add peer: %s (%d)", ether_ntoa(src), status);
		return RX_IGNORE;
	}
	status = awdl_peer_get(state->peers.peers, src, &peer);
	if (status < 0) {
		log_warn("awdl_action: could not find peer: %s (%d)", ether_ntoa(src), status);
		return RX_IGNORE; /* FIXME happens sometimes, not sure why */
	}
	if (!peer->rssi_known) { /* first frame of a new peer */
		peer->rssi_avg8 = (int32_t) rssi * 8;
		peer->rssi_known = 1;
	}

	log_trace("awdl_action: receive %s from %s (rssi %d)", awdl_frame_as_str(subtype), ether_ntoa(&peer->addr), rssi);

	while ((len = read_tlv(frame, 0, &tlv_type, &tlv_len, &tlv_value)) > 0) {
		const struct buf *tlv_buf = buf_new_const(tlv_value, tlv_len);
		int result;
		if (!tlv_buf)
			return RX_NO_MEMORY;
		result = awdl_handle_tlv(peer, tlv_type, tlv_buf, state, tsft);
		if (result >= 0 && state->tlv_cb)
			state->tlv_cb(peer, tlv_type, tlv_buf, state, state->tlv_cb_data);
		buf_free(tlv_buf);
		if (result < 0) {
			log_warn("awdl_action: parsing error %s", awdl_tlv_as_str(tlv_type));
			return RX_UNEXPECTED_FORMAT;
		}
		buf_strip(frame, len);
	}

	/* iPhones end some MIFs with zero padding. The loop above reads it as
	 * empty TLVs until fewer bytes than a TLV header are left: strip those
	 * too. A nonzero remainder is still a truncated TLV. */
	if (buf_len(frame) < 3) {
		int zeros = 0;
		while (zeros < buf_len(frame) && buf_data(frame)[zeros] == 0)
			zeros++;
		if (zeros == buf_len(frame))
			buf_strip(frame, zeros);
	}

	if (buf_len(frame) > 0) {
		log_debug("awdl_action: unexpected bytes (%d) at end of frame", buf_len(frame));
		return RX_UNEXPECTED_FORMAT;
	}

	if (subtype == AWDL_ACTION_MIF)
		peer->sent_mif = 1;

	/* update peer info after parsing all TLVs */
	awdl_peer_add(state->peers.peers, src, tsft, state->peer_cb, state->peer_cb_data);

	return RX_OK;
}

int llc_parse(const struct buf *frame, struct llc_hdr *llc) {
	uint16_t pid; /* to not take pointer of packed llc->pid */
	READ_U8(frame, 0, &llc->dsap);
	READ_U8(frame, 1, &llc->ssap);
	READ_U8(frame, 2, &llc->control);
	READ_BYTES_COPY(frame, 3, (uint8_t *) &llc->oui, 3);
	READ_BE16(frame, 6, &pid);
	llc->pid = pid;
	return RX_OK;
wire_error:
	return RX_TOO_SHORT;
}

int awdl_valid_llc_header(const struct buf *frame) {
	struct llc_hdr llc;

	if (llc_parse(frame, &llc) < 0) {
		log_warn("llc: frame to short");
		return 0;
	}

	if (llc.dsap != 0xaa || llc.ssap != 0xaa || llc.control != 0x03 ||
	    llc.pid != AWDL_LLC_PROTOCOL_ID || memcmp(&llc.oui, &AWDL_OUI, sizeof(llc.oui))) {
		log_warn("llc: invalid header (DSAP %#04x, SSAP %#04x, control %#04x, OUI %02x:%02x:%02x, PID %#06x",
		         llc.dsap, llc.ssap, llc.control,
		         llc.oui.byte[0], llc.oui.byte[1], llc.oui.byte[2], llc.pid);
		return 0;
	}
	return 1;
}

int awdl_rx_data(const struct buf *frame, struct buf ***out, struct buf **out_end, const struct ether_addr *src,
                 const struct ether_addr *dst, struct awdl_state *state) {
	uint16_t ether_type;
	struct awdl_peer *peer;
	struct buf *ethernet;
	int payload_offset = sizeof(struct llc_hdr) + sizeof(struct awdl_data);
	int payload_length;

	log_trace("awdl_data: receive from %s", ether_ntoa(src));
	state->stats.rx_data++;
	if (awdl_peer_get(state->peers.peers, src, &peer) != PEERS_OK)
		return RX_IGNORE_PEER;
	if (!awdl_valid_llc_header(frame))
		return RX_UNEXPECTED_FORMAT;
	if (buf_len(frame) < payload_offset)
		return RX_TOO_SHORT;
	payload_length = buf_len(frame) - payload_offset;
	if (payload_length > ETHER_MAX_LEN - ETHER_HDR_LEN)
		return RX_TOO_LARGE;
	if (!out || !*out || !out_end || *out >= out_end)
		return RX_TOO_MANY_FRAMES;
	if (read_be16(frame, sizeof(struct llc_hdr) + 6, &ether_type) < 0)
		return RX_TOO_SHORT;

	/* Allocate only bytes that will be initialized and delivered to the host.
	 * A fixed ETHER_MAX_LEN buffer used to forward an uninitialized tail. */
	ethernet = buf_new_owned(ETHER_HDR_LEN + payload_length);
	if (!ethernet)
		return RX_NO_MEMORY;
	if (write_ether_addr(ethernet, 0, dst) < 0 ||
	    write_ether_addr(ethernet, ETHER_ADDR_LEN, src) < 0 ||
	    write_be16(ethernet, 2 * ETHER_ADDR_LEN, ether_type) < 0 ||
	    write_bytes(ethernet, ETHER_HDR_LEN, buf_data(frame) + payload_offset, payload_length) < 0) {
		buf_free(ethernet);
		return RX_UNEXPECTED_FORMAT;
	}
	**out = ethernet;
	(*out)++;
	buf_strip(frame, payload_offset);

	/* Valid data keeps peers alive during transfers with sparse action frames. */
	peer->last_update = clock_time_us();
	return RX_OK;
}

int awdl_rx_data_amsdu(const struct buf *frame, struct buf ***out, struct buf **out_end, const struct ether_addr *src __attribute__((unused)),
                       const struct ether_addr *dst __attribute__((unused)), struct awdl_state *state) {
	/* Iterate over all subframes */
	while (buf_len(frame) > 0) {
		struct ether_addr src_a, dst_a;
		uint16_t len_a;
		int err;
		const struct buf *subframe;
		READ_ETHER_ADDR(frame, 0, &dst_a);
		READ_ETHER_ADDR(frame, 6, &src_a);
		READ_BE16(frame, 12, &len_a);
		BUF_STRIP(frame, 14); /* strip subframe header */
		if (len_a > buf_len(frame))
			return RX_TOO_SHORT;
		/* create subview of buf */
		subframe = buf_new_const(buf_data(frame), len_a);
		if (!subframe)
			return RX_NO_MEMORY;
		err = awdl_rx_data(subframe, out, out_end, &src_a, &dst_a, state);
		buf_free(subframe);
		if (err < 0)
			return err;
		BUF_STRIP(frame, len_a); /* strip frame */
		if (buf_len(frame) > 0)
			BUF_STRIP(frame, (4 - ((14 + len_a) % 4)) % 4); /* strip padding */
	}
	return RX_OK;
wire_error:
	return RX_TOO_SHORT;
}

static int radiotap_parse(const struct buf *frame, signed char *rssi, uint8_t *flags, uint64_t *tsft) {
	struct ieee80211_radiotap_iterator iter;
	uint16_t header_length;
	int err;

	if (read_le16(frame, 2, &header_length) < 0 ||
	    header_length < sizeof(struct ieee80211_radiotap_header) || header_length > buf_len(frame))
		return RX_UNEXPECTED_FORMAT;

	err = ieee80211_radiotap_iterator_init(&iter, (struct ieee80211_radiotap_header *) buf_data(frame),
	                                       buf_len(frame), NULL);
	if (err < 0)
		return RX_UNEXPECTED_FORMAT;

	while (!(err = ieee80211_radiotap_iterator_next(&iter))) {
		if (iter.is_radiotap_ns) {
			switch (iter.this_arg_index) {
				case IEEE80211_RADIOTAP_TSFT: /* https://www.radiotap.org/fields/TSFT.html */
					if (tsft) {
						uint64_t raw;
						memcpy(&raw, iter.this_arg, sizeof(raw));
						*tsft = le64toh(raw);
					}
					break;
				case IEEE80211_RADIOTAP_FLAGS:
					if (flags)
						*flags = *(unsigned char *) iter.this_arg;
					break;
				case IEEE80211_RADIOTAP_DBM_ANTSIGNAL: /* https://www.radiotap.org/fields/Antenna%20signal.html */
					if (rssi)
						*rssi = *(signed char *) iter.this_arg;
				default:
					/* ignore */
					break;
			}
		}
	}

	if (err != -ENOENT)
		return RX_UNEXPECTED_FORMAT;

	return RX_OK;
}

static int check_fcs(const struct buf *frame, uint8_t radiotap_flags) {
	if (radiotap_flags & IEEE80211_RADIOTAP_F_BADFCS)
		return -1;
	if (radiotap_flags & IEEE80211_RADIOTAP_F_FCS)
		BUF_TAKE(frame, 4); /* strip FCS */
	return 0;
wire_error:
	return -1;
}

int awdl_rx(const struct buf *frame, struct buf ***data_frame, struct buf **data_end, struct awdl_state *state) {
	const struct ieee80211_hdr *ieee80211;
	const struct ether_addr *from, *to;
	uint16_t fc, qosc; /* frame and QoS control */
	signed char rssi = 0;
	uint64_t tsft;
	uint8_t flags = 0;

	tsft = clock_time_us(); /* TODO Radiotap TSFT is more accurate but then need to access TSF in clock_time_us() */
	if (radiotap_parse(frame, &rssi, &flags, NULL /* &tsft */) < 0)
		return RX_UNEXPECTED_FORMAT;
	BUF_STRIP(frame, le16toh(((const struct ieee80211_radiotap_header *) buf_data(frame))->it_len));

	if (check_fcs(frame, flags)) /* note that if no flags are present (flags==0), frames will pass */
		return RX_IGNORE_FAILED_CRC;

	READ_BYTES(frame, 0, NULL, sizeof(struct ieee80211_hdr));

	ieee80211 = (const struct ieee80211_hdr *) (buf_data(frame));
	from = &ieee80211->addr2;
	to = &ieee80211->addr1;
	fc = le16toh(ieee80211->frame_control);

	if (!memcmp(from, &state->self_address, sizeof(struct ether_addr)))
		return RX_IGNORE_FROM_SELF; /* TODO ignore frames from self, should be filtered at pcap level */

	//if (!(to->ether_addr_octet[0] & 0x01) && memcmp(to, &state->self_address, sizeof(struct ether_addr)))
	//	return RX_IGNORE_NOPROMISC; /* neither broadcast/multicast nor unicast to me */

	BUF_STRIP(frame, sizeof(struct ieee80211_hdr));

	/* Processing based on frame type and subtype */
	switch (fc & (IEEE80211_FCTL_FTYPE | IEEE80211_FCTL_STYPE)) {
		case IEEE80211_FTYPE_MGMT | IEEE80211_STYPE_ACTION:
			return awdl_rx_action(frame, rssi, tsft, from, to, state);
		case IEEE80211_FTYPE_DATA | IEEE80211_STYPE_DATA | IEEE80211_STYPE_QOS_DATA:
			READ_LE16(frame, 0, &qosc);
			BUF_STRIP(frame, IEEE80211_QOS_CTL_LEN);
			/* TODO should handle block acks if required (IEEE80211_QOS_CTL_ACK_POLICY_XYZ) */
			if (qosc & IEEE80211_QOS_CTL_A_MSDU_PRESENT)
				return awdl_rx_data_amsdu(frame, data_frame, data_end, from, to, state);
			/* else fall through */
		case IEEE80211_FTYPE_DATA | IEEE80211_STYPE_DATA:
			return awdl_rx_data(frame, data_frame, data_end, from, to, state);
		default:
			log_warn("ieee80211: cannot handle type %x and subtype %x of received frame from %s",
			         fc & IEEE80211_FCTL_FTYPE, fc & IEEE80211_FCTL_STYPE, ether_ntoa(from));
			return RX_UNEXPECTED_TYPE;
	}
wire_error:
	return RX_TOO_SHORT;
}
