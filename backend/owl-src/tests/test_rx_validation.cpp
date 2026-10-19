/* Regression coverage for hostile wire sizes and receive-side frame validation. */
extern "C" {
#include "rx.h"
#include "wire.h"
#include "tx.h"
}

#include "gtest/gtest.h"
#include <climits>
#include <cstring>
#include <vector>

class WireView {
public:
	explicit WireView(const std::vector<uint8_t>& bytes)
		: value(buf_new_const(bytes.data(), static_cast<int>(bytes.size()))) {}
	WireView(const uint8_t *bytes, int length) : value(buf_new_const(bytes, length)) {}
	~WireView() { buf_free(value); }
	const struct buf *value;
};

TEST(WireValidation, InvalidRangesNeverReachMemoryOperations) {
	struct buf *buffer = buf_new_owned(8);
	ASSERT_NE(buffer, nullptr);
	const uint8_t input[8] = {};
	uint8_t output[8] = {};
	const int invalid[][2] = {{-1, 1}, {0, -1}, {1, INT_MAX}, {INT_MAX, 4}, {9, 0}};
	for (const auto& range : invalid) {
		EXPECT_EQ(read_bytes(buffer, range[0], nullptr, range[1]), OUT_OF_BOUNDS);
		EXPECT_EQ(read_bytes_copy(buffer, range[0], output, range[1]), OUT_OF_BOUNDS);
		EXPECT_EQ(write_bytes(buffer, range[0], input, range[1]), OUT_OF_BOUNDS);
	}
	EXPECT_EQ(read_le32(buffer, INT_MAX, nullptr), OUT_OF_BOUNDS);
	EXPECT_EQ(write_be32(buffer, INT_MAX, 42), OUT_OF_BOUNDS);
	EXPECT_EQ(read_tlv(buffer, INT_MAX, nullptr, nullptr, nullptr), OUT_OF_BOUNDS);
	EXPECT_EQ(read_int_string(buffer, INT_MAX, nullptr, 4), OUT_OF_BOUNDS);
	buf_free(buffer);
}

TEST(WireValidation, UnalignedIntegersPreserveWireByteOrder) {
	struct buf *buffer = buf_new_owned(12);
	ASSERT_NE(buffer, nullptr);
	EXPECT_EQ(write_le16(buffer, 1, 0x1234), 2);
	EXPECT_EQ(write_be32(buffer, 3, 0x12345678), 4);
	EXPECT_EQ(write_le32(buffer, 7, 0x12345678), 4);
	uint16_t short_value = 0;
	uint32_t long_value = 0;
	EXPECT_EQ(read_le16(buffer, 1, &short_value), 2);
	EXPECT_EQ(short_value, 0x1234);
	EXPECT_EQ(read_be32(buffer, 3, &long_value), 4);
	EXPECT_EQ(long_value, 0x12345678U);
	EXPECT_EQ(read_le32(buffer, 7, &long_value), 4);
	EXPECT_EQ(long_value, 0x12345678U);
	const uint8_t expected[] = {0x34, 0x12, 0x12, 0x34, 0x56, 0x78, 0x78, 0x56, 0x34, 0x12};
	EXPECT_EQ(std::memcmp(buf_data(buffer) + 1, expected, sizeof(expected)), 0);
	buf_free(buffer);
}

TEST(WireValidation, TruncatedStringIsRejectedEvenWhenDestinationIsShort) {
	const uint8_t bytes[] = {100, 'o', 'w', 'l'};
	WireView frame(bytes, sizeof(bytes));
	char output[4] = "old";
	EXPECT_EQ(read_int_string(frame.value, 0, output, 3), OUT_OF_BOUNDS);
	EXPECT_STREQ(output, "old");
	EXPECT_EQ(read_int_string(frame.value, 0, output, -1), OUT_OF_BOUNDS);
	EXPECT_EQ(read_int_string(frame.value, 0, nullptr, 0), OUT_OF_BOUNDS);
}

TEST(WireValidation, EmptyAndInvalidBuffersAreHandledConsistently) {
	EXPECT_EQ(buf_new_owned(-1), nullptr);
	EXPECT_EQ(buf_new_const(nullptr, 1), nullptr);
	const struct buf *empty = buf_new_const(nullptr, 0);
	ASSERT_NE(empty, nullptr);
	EXPECT_EQ(buf_strip(empty, 0), 0);
	EXPECT_EQ(read_bytes(empty, 0, nullptr, 0), 0);
	EXPECT_EQ(read_u8(empty, 0, nullptr), OUT_OF_BOUNDS);
	buf_free(empty);
	buf_free(nullptr);
}

class RxValidation : public ::testing::Test {
protected:
	struct awdl_state state;
	struct ether_addr self = {{2, 0, 0, 0, 0, 1}};
	struct ether_addr sender = {{2, 0, 0, 0, 0, 2}};
	struct awdl_peer *peer = nullptr;
	struct buf *output[4] = {};
	struct buf **cursor = output;

	void SetUp() override {
		struct awdl_chan channel = CHAN_OPCLASS_6;
		awdl_init_state(&state, "receiver", &self, channel, 0);
		state.filter_rssi = 0;
		ASSERT_EQ(awdl_peer_add(state.peers.peers, &sender, 123, nullptr, nullptr), PEERS_OK);
		ASSERT_EQ(awdl_peer_get(state.peers.peers, &sender, &peer), PEERS_OK);
	}
	void TearDown() override {
		for (struct buf **item = output; item < cursor; ++item)
			buf_free(*item);
		awdl_peers_free(state.peers.peers);
	}
	std::vector<uint8_t> Data(size_t payload_length) {
		std::vector<uint8_t> data(sizeof(struct llc_hdr) + sizeof(struct awdl_data) + payload_length);
		llc_init_awdl_hdr(data.data());
		awdl_init_data(data.data() + sizeof(struct llc_hdr), &state);
		for (size_t i = 0; i < payload_length; ++i)
			data[sizeof(struct llc_hdr) + sizeof(struct awdl_data) + i] = static_cast<uint8_t>(i);
		return data;
	}
	std::vector<uint8_t> Action(const std::vector<uint8_t>& tlvs) {
		std::vector<uint8_t> data(sizeof(struct awdl_action));
		awdl_init_action(data.data(), AWDL_ACTION_MIF);
		data.insert(data.end(), tlvs.begin(), tlvs.end());
		return data;
	}
	int ReceiveData(const std::vector<uint8_t>& data) {
		WireView frame(data);
		return awdl_rx_data(frame.value, &cursor, output + 4, &sender, &self, &state);
	}
	int ReceiveAction(const std::vector<uint8_t>& data) {
		WireView frame(data);
		return awdl_rx_action(frame.value, -40, 1000, &sender, &self, &state);
	}
};

TEST_F(RxValidation, EthernetOutputContainsOnlyInitializedHeaderAndPayload) {
	for (size_t length : {0U, 7U, 1500U}) {
		auto data = Data(length);
		ASSERT_EQ(ReceiveData(data), RX_OK);
		const struct buf *ethernet = *(cursor - 1);
		ASSERT_EQ(buf_len(ethernet), static_cast<int>(ETHER_HDR_LEN + length));
		EXPECT_EQ(std::memcmp(buf_data(ethernet), &self, ETHER_ADDR_LEN), 0);
		EXPECT_EQ(std::memcmp(buf_data(ethernet) + ETHER_ADDR_LEN, &sender, ETHER_ADDR_LEN), 0);
		EXPECT_EQ(buf_data(ethernet)[12], 0x86);
		EXPECT_EQ(buf_data(ethernet)[13], 0xdd);
		EXPECT_EQ(std::memcmp(buf_data(ethernet) + ETHER_HDR_LEN,
		                      data.data() + sizeof(struct llc_hdr) + sizeof(struct awdl_data), length), 0);
	}
}

TEST_F(RxValidation, OversizedPayloadDoesNotPublishAnOutputOrRefreshThePeer) {
	EXPECT_EQ(ReceiveData(Data(ETHER_MAX_LEN - ETHER_HDR_LEN + 1)), RX_TOO_LARGE);
	EXPECT_EQ(cursor, output);
	EXPECT_EQ(peer->last_update, 123U);
}

TEST_F(RxValidation, ForeignSnapOuiDoesNotRefreshThePeer) {
	auto data = Data(10);
	data[3] = 0x01;
	EXPECT_EQ(ReceiveData(data), RX_UNEXPECTED_FORMAT);
	EXPECT_EQ(cursor, output);
	EXPECT_EQ(peer->last_update, 123U);
}

TEST_F(RxValidation, EveryTruncatedDataHeaderIsRejectedWithoutAnOutput) {
	auto data = Data(0);
	for (size_t length = 0; length < data.size(); ++length) {
		std::vector<uint8_t> truncated(data.begin(), data.begin() + length);
		EXPECT_LT(ReceiveData(truncated), 0) << "prefix " << length;
		EXPECT_EQ(cursor, output);
	}
	EXPECT_EQ(peer->last_update, 123U);
}

TEST_F(RxValidation, FullOutputArrayIsRejectedWithoutChangingIt) {
	auto data = Data(7);
	WireView frame(data);
	EXPECT_EQ(awdl_rx_data(frame.value, &cursor, output, &sender, &self, &state), RX_TOO_MANY_FRAMES);
	EXPECT_EQ(cursor, output);
	EXPECT_EQ(peer->last_update, 123U);
}

static void CountTlv(struct awdl_peer *, uint8_t, const struct buf *, struct awdl_state *, void *count) {
	++*static_cast<int *>(count);
}

TEST_F(RxValidation, MalformedActionDoesNotPartlyUpdatePeerClockOrCallbacks) {
	int callbacks = 0;
	state.tlv_cb = CountTlv;
	state.tlv_cb_data = &callbacks;
	state.election.sync_addr = sender;
	peer->version = 0x12;
	const struct awdl_sync_state before = state.sync;
	std::vector<uint8_t> tlvs = {AWDL_VERSION_TLV, 2, 0, 0x34, 1,
	                             AWDL_SYNCHRONIZATON_PARAMETERS_TLV, 31, 0};
	tlvs.resize(tlvs.size() + 31, 0);
	// A complete envelope containing a truncated election value follows valid TLVs.
	tlvs.insert(tlvs.end(), {AWDL_ELECTION_PARAMETERS_V2_TLV, 1, 0, 0});
	EXPECT_EQ(ReceiveAction(Action(tlvs)), RX_UNEXPECTED_FORMAT);
	EXPECT_EQ(peer->version, 0x12);
	EXPECT_EQ(peer->last_update, 123U);
	EXPECT_EQ(peer->has_sync_offset, 0);
	EXPECT_EQ(peer->sent_mif, 0);
	EXPECT_EQ(std::memcmp(&before, &state.sync, sizeof(before)), 0);
	EXPECT_EQ(callbacks, 0);
	EXPECT_EQ(state.stats.rx_action, 0U);
}

TEST_F(RxValidation, MalformedTailDoesNotCreateANewPeer) {
	struct ether_addr stranger = {{2, 0, 0, 0, 0, 3}};
	auto data = Action({AWDL_VERSION_TLV, 2, 0, 0x34, 1, 0, 1});
	WireView frame(data);
	EXPECT_EQ(awdl_rx_action(frame.value, -40, 1000, &stranger, &self, &state), RX_UNEXPECTED_FORMAT);
	EXPECT_EQ(awdl_peers_length(state.peers.peers), 1);
}

TEST_F(RxValidation, UnknownCompleteTlvAndIphoneZeroPaddingRemainAccepted) {
	auto data = Action({AWDL_VERSION_TLV, 2, 0, 0x34, 1, 99, 2, 0, 0xab, 0xcd, 0, 0, 0, 0, 0, 0, 0, 0});
	EXPECT_EQ(ReceiveAction(data), RX_OK);
	EXPECT_EQ(peer->version, 0x34);
	EXPECT_TRUE(peer->sent_mif);
	EXPECT_TRUE(peer->is_valid);
}

TEST_F(RxValidation, TruncatedElectionValueDoesNotOverwritePreviouslyKnownFields) {
	std::vector<uint8_t> data(39, 0x77);
	WireView frame(data);
	const struct awdl_election_state before = peer->election;
	EXPECT_EQ(awdl_handle_election_params_v2_tlv(peer, frame.value, &state), RX_TOO_SHORT);
	EXPECT_EQ(std::memcmp(&before, &peer->election, sizeof(before)), 0);
	EXPECT_FALSE(peer->supports_v2);
}

TEST_F(RxValidation, TruncatedDataPathDoesNotPartlyUpdateCountryCode) {
	const uint8_t data[] = {0x02, 0x01, 'I', 'T', 0, 1}; // country then missing infrastructure address
	WireView frame(data, sizeof(data));
	std::memcpy(peer->country_code, "US", 3);
	EXPECT_EQ(awdl_handle_data_path_state_tlv(peer, frame.value, &state), RX_TOO_SHORT);
	EXPECT_STREQ(peer->country_code, "US");
}

TEST_F(RxValidation, SimpleChannelEncodingClearsUnusedBytes) {
	std::vector<uint8_t> data = {15, AWDL_CHAN_ENC_SIMPLE, 0,
	                            static_cast<uint8_t>(state.sync.presence_mode - 1), 0xff, 0xff};
	data.resize(6 + AWDL_CHANSEQ_LENGTH, 6);
	WireView frame(data);
	std::memset(peer->sequence, 0xff, sizeof(peer->sequence));
	ASSERT_EQ(awdl_handle_chanseq_tlv(peer, frame.value, &state), RX_OK);
	for (const auto& channel : peer->sequence) {
		EXPECT_EQ(channel.val[0], 6);
		EXPECT_EQ(channel.val[1], 0);
	}
}

TEST_F(RxValidation, RadiotapCannotDeclareALengthSmallerThanItsHeader) {
	for (uint8_t length = 0; length < 8; ++length) {
		std::vector<uint8_t> data(48, 0);
		data[2] = length;
		WireView frame(data);
		EXPECT_EQ(awdl_rx(frame.value, &cursor, output + 4, &state), RX_UNEXPECTED_FORMAT);
		EXPECT_EQ(cursor, output);
	}
}

TEST_F(RxValidation, AnUnalignedCompleteRadioFrameIsDecoded) {
	std::vector<uint8_t> data(1 + 8 + sizeof(struct ieee80211_hdr), 0);
	data[3] = 8; // radiotap length relative to the unaligned frame start
	struct ieee80211_hdr header = {};
	header.frame_control = htole16(IEEE80211_FTYPE_DATA | IEEE80211_STYPE_DATA);
	header.addr1 = self;
	header.addr2 = sender;
	std::memcpy(data.data() + 9, &header, sizeof(header));
	auto payload = Data(17);
	data.insert(data.end(), payload.begin(), payload.end());
	WireView frame(data.data() + 1, static_cast<int>(data.size() - 1));
	ASSERT_EQ(awdl_rx(frame.value, &cursor, output + 4, &state), RX_OK);
	ASSERT_EQ(cursor, output + 1);
	EXPECT_EQ(buf_len(output[0]), ETHER_HDR_LEN + 17);
}

TEST_F(RxValidation, ArpaSuffixAndFrameChecksumAllowUnalignedDestinations) {
	std::vector<uint8_t> data(128, 0);
	std::strcpy(state.name, "owl");
	int length = awdl_init_arpa_tlv(data.data() + 1, &state);
	ASSERT_EQ(length, 10);
	EXPECT_EQ(data[1 + length - 2], 0xc0);
	EXPECT_EQ(data[1 + length - 1], 0x0c);
	// Standard CRC32 check value, deliberately written at an odd offset.
	std::memcpy(data.data(), "123456789", 9);
	ASSERT_EQ(ieee80211_add_fcs(data.data(), data.data() + 9), 4);
	const uint8_t expected[] = {0x26, 0x39, 0xf4, 0xcb};
	EXPECT_EQ(std::memcmp(data.data() + 9, expected, sizeof(expected)), 0);
}
