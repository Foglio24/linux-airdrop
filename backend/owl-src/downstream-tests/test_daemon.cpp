/* Exercise the real libev callbacks with deterministic host/radio I/O. No TAP,
 * nl80211 requests, radio privileges or timing sleeps are used. */
extern "C" {
#include "core.h"
#include "schedule.h"
#include "tx.h"
#include "wire.h"
#include "log.h"
#include <fcntl.h>
#include <unistd.h>
}
#include "gtest/gtest.h"
#include <cerrno>
#include <cstring>
#include <deque>
#include <vector>

static int radio_result, radio_calls, host_calls;
static uint64_t test_now;
static std::deque<std::vector<uint8_t>> incoming;
static std::vector<uint8_t> last_radio, last_host;

extern "C" int __wrap_wlan_send(const io_state *, const uint8_t *bytes, int length) {
    radio_calls++;
    last_radio.assign(bytes, bytes + length);
    return radio_result;
}
extern "C" int __wrap_host_send(const io_state *, const uint8_t *bytes, int length) {
    host_calls++;
    last_host.assign(bytes, bytes + length);
    return 0;
}
extern "C" int __wrap_host_recv(const io_state *, uint8_t *bytes, int *length) {
    if (incoming.empty()) return -EAGAIN;
    auto frame = incoming.front();
    incoming.pop_front();
    if (frame.size() > static_cast<size_t>(*length)) return -EMSGSIZE;
    memcpy(bytes, frame.data(), frame.size());
    *length = frame.size();
    return 0;
}
extern "C" uint64_t __wrap_clock_time_us() { return test_now; }
extern "C" int __real_host_send(const io_state *, const uint8_t *, int);
extern "C" int __real_host_recv(const io_state *, uint8_t *, int *);

class Daemon : public ::testing::Test {
protected:
    daemon_state state{};
    struct ev_loop *loop = nullptr;
    ether_addr self{{0x02, 0, 0, 0, 0, 1}};
    ether_addr phone{{0x02, 0, 0, 0, 0, 2}};
    ether_addr multicast{{0x33, 0x33, 0, 0, 0, 1}};
    awdl_peer *peer = nullptr;

    void SetUp() override {
        radio_result = radio_calls = host_calls = 0;
        incoming.clear(); last_radio.clear(); last_host.clear();
        test_now = (4 * 64 + 30) * 1024;
        log_set_level(LOG_ERR);
        state.io.host_fd = open("/dev/null", O_RDWR);
        ASSERT_GE(state.io.host_fd, 0);
        state.io.wlan_fd = -1;
        awdl_chan channel = CHAN_OPCLASS_44;
        awdl_init_state(&state.awdl_state, "test", &self, channel, 0);
        ieee80211_init_state(&state.ieee80211_state);
        state.awdl_state.channel.fixed_radio = 1;
        awdl_peer_add(state.awdl_state.peers.peers, &phone, 0, nullptr, nullptr);
        ASSERT_EQ(awdl_peer_get(state.awdl_state.peers.peers, &phone, &peer), PEERS_OK);
        peer->sequence_enc = AWDL_CHAN_ENC_OPCLASS;
        peer->has_sequence = 1;
        for (auto &slot : peer->sequence) slot = channel;
        state.tx_queue_multicast = circular_buf_init(16);
        loop = ev_loop_new(EVBACKEND_POLL);
        ASSERT_NE(loop, nullptr);
        state.ev_state.loop = loop;
        ev_io_init(&state.ev_state.read_host, host_device_ready, state.io.host_fd, EV_READ);
        state.ev_state.read_host.data = &state;
        ev_io_start(loop, &state.ev_state.read_host);
        ev_timer_init(&state.ev_state.tx_timer, awdl_send_unicast, 0, 0);
        state.ev_state.tx_timer.data = &state;
        ev_timer_init(&state.ev_state.tx_mcast_timer, awdl_send_multicast, 0, 0);
        state.ev_state.tx_mcast_timer.data = &state;
    }
    void TearDown() override {
        awdl_free(&state);
        ev_loop_destroy(loop);
    }
    std::vector<uint8_t> ethernet(const ether_addr &destination) {
        std::vector<uint8_t> frame(14 + 40, 0);
        memcpy(frame.data(), &destination, 6);
        memcpy(frame.data() + 6, &self, 6);
        frame[12] = 0x86; frame[13] = 0xdd; frame[14] = 0x60;
        return frame;
    }
    buf *packet(const ether_addr &destination) {
        auto bytes = ethernet(destination);
        buf *frame = buf_new_owned(bytes.size());
        write_bytes(frame, 0, bytes.data(), bytes.size());
        return frame;
    }
    void unicast() { awdl_send_unicast(loop, &state.ev_state.tx_timer, 0); }
    void mcast() { awdl_send_multicast(loop, &state.ev_state.tx_mcast_timer, 0); }
};

TEST_F(Daemon, UnicastRetriesAnIntactPacketAndCountsOnlySuccess) {
    state.next = packet(phone);
    const buf *original = state.next;
    auto bytes = ethernet(phone);
    radio_result = -EAGAIN;
    unicast();
    ASSERT_EQ(state.next, original);
    EXPECT_EQ(buf_len(state.next), bytes.size());
    EXPECT_EQ(memcmp(buf_data(state.next), bytes.data(), bytes.size()), 0);
    EXPECT_EQ(state.awdl_state.stats.tx_data, 0u);
    EXPECT_EQ(state.awdl_state.stats.gate_sent, 0u);
    EXPECT_EQ(state.awdl_state.stats.gate_queue_full, 1u);
    EXPECT_TRUE(ev_is_active(&state.ev_state.tx_timer));
    EXPECT_FALSE(ev_is_active(&state.ev_state.read_host));
    radio_result = 0; test_now += 500;
    unicast();
    EXPECT_EQ(state.next, nullptr);
    EXPECT_EQ(state.awdl_state.stats.tx_data, 1u);
    EXPECT_EQ(state.awdl_state.stats.tx_data_unicast, 1u);
    EXPECT_EQ(state.awdl_state.stats.gate_sent, 1u);
    EXPECT_EQ(state.awdl_state.stats.tx_slot[4], 1u);
    EXPECT_EQ(state.awdl_state.stats.held_us, 500u);
    EXPECT_FALSE(ev_is_active(&state.ev_state.tx_timer));
    EXPECT_TRUE(ev_is_active(&state.ev_state.read_host));
}

TEST_F(Daemon, UnicastPermanentFailureIsNotCountedAsSent) {
    state.next = packet(phone); radio_result = -EIO;
    unicast();
    EXPECT_EQ(state.next, nullptr);
    EXPECT_EQ(state.awdl_state.stats.tx_data, 0u);
    EXPECT_EQ(state.awdl_state.stats.tx_data_unicast, 0u);
    EXPECT_EQ(state.awdl_state.stats.gate_sent, 0u);
    EXPECT_EQ(state.awdl_state.stats.tx_data_failed, 1u);
}

TEST_F(Daemon, RetryRechecksPeerChannelBeforeInjecting) {
    state.next = packet(phone); radio_result = -EAGAIN;
    unicast();
    ASSERT_EQ(radio_calls, 1);
    peer->sequence[4] = CHAN_NULL;
    radio_result = 0; test_now += 500;
    unicast();
    EXPECT_EQ(radio_calls, 1);
    EXPECT_NE(state.next, nullptr);
    EXPECT_EQ(state.awdl_state.stats.gate_wait_channel, 1u);
}

TEST_F(Daemon, EndGuardWaitsForNextWindowPlusStartGuard) {
    state.next = packet(phone);
    peer->sequence[5] = CHAN_NULL;
    test_now = (4 * 64 + 62) * 1024;
    unicast();
    EXPECT_EQ(radio_calls, 0);
    EXPECT_EQ(state.awdl_state.stats.gate_wait_end, 1u);
    EXPECT_NEAR(ev_timer_remaining(loop, &state.ev_state.tx_timer), 5 * 1024 / 1e6, 1e-8);
}

TEST_F(Daemon, MulticastRetriesWithoutLosingQueueOwnership) {
    buf *first = packet(multicast);
    circular_buf_put2(state.tx_queue_multicast, first);
    circular_buf_put2(state.tx_queue_multicast, packet(multicast));
    radio_result = -EAGAIN;
    mcast();
    EXPECT_EQ(circular_buf_size(state.tx_queue_multicast), 2u);
    void *head;
    circular_buf_get(state.tx_queue_multicast, &head, 1);
    EXPECT_EQ(head, first);
    EXPECT_EQ(state.awdl_state.stats.tx_data_multicast, 0u);
    radio_result = 0;
    mcast();
    EXPECT_EQ(circular_buf_size(state.tx_queue_multicast), 1u);
    EXPECT_EQ(state.awdl_state.stats.tx_data_multicast, 1u);
    mcast();
    EXPECT_TRUE(circular_buf_empty(state.tx_queue_multicast));
    EXPECT_EQ(state.awdl_state.stats.tx_data_multicast, 2u);
    EXPECT_FALSE(ev_is_active(&state.ev_state.tx_mcast_timer));
}

TEST_F(Daemon, MulticastFailureDoesNotBlockFollowingPacketOrCountSuccess) {
    circular_buf_put2(state.tx_queue_multicast, packet(multicast));
    radio_result = -EIO;
    mcast();
    EXPECT_TRUE(circular_buf_empty(state.tx_queue_multicast));
    EXPECT_EQ(state.awdl_state.stats.tx_data_multicast, 0u);
    EXPECT_EQ(state.awdl_state.stats.tx_data_failed, 1u);
}

TEST_F(Daemon, ActionCountersReflectInjectionResult) {
    radio_result = -EIO;
    awdl_send_action(&state, AWDL_ACTION_PSF);
    EXPECT_EQ(state.awdl_state.stats.tx_action, 0u);
    EXPECT_EQ(state.awdl_state.stats.tx_action_failed, 1u);
    radio_result = 0;
    awdl_send_action(&state, AWDL_ACTION_PSF);
    EXPECT_EQ(state.awdl_state.stats.tx_action, 1u);
}

TEST_F(Daemon, ReaderPausesForUnicastAndResumesAfterItDrains) {
    incoming.push_back(ethernet(phone));
    incoming.push_back(ethernet(phone));
    peer->sequence[4] = CHAN_NULL;
    host_device_ready(loop, &state.ev_state.read_host, EV_READ);
    EXPECT_EQ(incoming.size(), 1u);
    EXPECT_NE(state.next, nullptr);
    EXPECT_FALSE(ev_is_active(&state.ev_state.read_host));
    peer->sequence[4] = CHAN_OPCLASS_44;
    unicast();
    EXPECT_TRUE(ev_is_active(&state.ev_state.read_host));
    host_device_ready(loop, &state.ev_state.read_host, EV_READ);
    EXPECT_TRUE(incoming.empty());
    EXPECT_EQ(state.awdl_state.stats.tx_data_unicast, 2u);
}

TEST_F(Daemon, ReaderPausesAtFullMulticastQueueAndResumesWhenSpaceReturns) {
    for (int n = 0; n < 20; n++) incoming.push_back(ethernet(multicast));
    radio_result = -EAGAIN;
    host_device_ready(loop, &state.ev_state.read_host, EV_READ);
    EXPECT_EQ(circular_buf_size(state.tx_queue_multicast), 16u);
    EXPECT_EQ(incoming.size(), 4u);
    EXPECT_FALSE(ev_is_active(&state.ev_state.read_host));
    radio_result = 0;
    mcast();
    EXPECT_EQ(circular_buf_size(state.tx_queue_multicast), 15u);
    EXPECT_TRUE(ev_is_active(&state.ev_state.read_host));
}

TEST_F(Daemon, ShortAndNonIPv6HostFramesAreDiscarded) {
    incoming.push_back(std::vector<uint8_t>(5, 0));
    auto other = ethernet(phone); other[12] = 0x08; other[13] = 0;
    incoming.push_back(other);
    incoming.push_back(ethernet(phone));
    host_device_ready(loop, &state.ev_state.read_host, EV_READ);
    EXPECT_TRUE(incoming.empty());
    EXPECT_EQ(radio_calls, 1);
}

TEST_F(Daemon, SelfDeliveryAndUnknownPeerReleasePendingFrame) {
    state.next = packet(self);
    unicast();
    EXPECT_EQ(host_calls, 1);
    EXPECT_EQ(last_host, ethernet(self));
    EXPECT_EQ(radio_calls, 0);
    ether_addr unknown{{0x02, 0, 0, 0, 0, 3}};
    state.next = packet(unknown);
    unicast();
    EXPECT_EQ(state.next, nullptr);
    EXPECT_EQ(state.awdl_state.stats.gate_non_peer, 1u);
    EXPECT_EQ(radio_calls, 0);
}

TEST_F(Daemon, CleanupDrainsPendingFramesAndIsIdempotent) {
    state.next = packet(phone);
    circular_buf_put2(state.tx_queue_multicast, packet(multicast));
    ev_timer_start(loop, &state.ev_state.tx_timer);
    awdl_free(&state);
    EXPECT_EQ(state.next, nullptr);
    EXPECT_EQ(state.tx_queue_multicast, nullptr);
    EXPECT_EQ(state.awdl_state.peers.peers, nullptr);
    EXPECT_EQ(state.io.host_fd, -1);
    EXPECT_FALSE(ev_is_active(&state.ev_state.read_host));
    EXPECT_FALSE(ev_is_active(&state.ev_state.tx_timer));
    awdl_free(&state);
}

TEST_F(Daemon, MalformedFrameDumpFailureDoesNotCrash) {
    state.dump = "/nonexistent-owl-test-directory/dump.pcap";
    uint8_t bytes[] = {0};
    pcap_pkthdr header{}; header.caplen = header.len = sizeof(bytes);
    awdl_receive_frame(reinterpret_cast<uint8_t *>(&state), &header, bytes);
    EXPECT_EQ(state.awdl_state.stats.rx_unknown, 1u);
}

TEST(Io, DescriptorZeroIsUsableAndEofIsAnError) {
    int saved_stdin = dup(STDIN_FILENO);
    ASSERT_GE(saved_stdin, 0);
    int pipes[2]; ASSERT_EQ(pipe(pipes), 0);
    ASSERT_EQ(dup2(pipes[0], STDIN_FILENO), STDIN_FILENO);
    close(pipes[0]);
    io_state io{}; io.host_fd = STDIN_FILENO;
    uint8_t sent = 42, received = 0;
    EXPECT_EQ(write(pipes[1], &sent, 1), 1);
    int length = 1;
    EXPECT_EQ(__real_host_recv(&io, &received, &length), 0);
    EXPECT_EQ(received, 42);
    close(pipes[1]); length = 1;
    EXPECT_EQ(__real_host_recv(&io, &received, &length), -EPIPE);
    EXPECT_EQ(dup2(saved_stdin, STDIN_FILENO), STDIN_FILENO);
    close(saved_stdin);
}

TEST(Io, AbsentDescriptorsAreRejected) {
    io_state io{}; io.host_fd = -1;
    uint8_t byte = 0; int length = 1;
    EXPECT_EQ(__real_host_send(&io, &byte, 1), -EINVAL);
    EXPECT_EQ(__real_host_recv(&io, &byte, &length), -EINVAL);
}
