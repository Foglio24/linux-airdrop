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

#include <errno.h>
#include <unistd.h>
#include <string.h>
#include <stdio.h>
#include <fcntl.h>
#include <net/if.h>
#include <sys/ioctl.h>
#ifndef __APPLE__
#include <linux/if_tun.h>
#else
#include <sys/sys_domain.h>
#include <sys/kern_control.h>
#include <netinet/in.h>
#include <netinet/in_var.h>
#endif /* __APPLE__ */
#include "io.h"
#include "netutils.h"

#include <log.h>
#include <wire.h>

static int open_nonblocking_device(const char *dev, pcap_t **pcap_handle, const struct ether_addr *bssid_filter) {
	char errbuf[PCAP_ERRBUF_SIZE];
	pcap_t *handle;
	struct bpf_program filter;
	int fd;
	char filter_str[128];

	handle = pcap_create(dev, errbuf);
	if (handle == NULL) {
		log_error("pcap: unable to open device %s (%s)", dev, errbuf);
		return -1;
	}

	pcap_set_snaplen(handle, 65535);
	pcap_set_promisc(handle, 1);
#ifdef __APPLE__
	/* On Linux, we activate monitor mode via nl80211 */
	pcap_set_rfmon(handle, 1);
#endif /* __APPLE__ */

	pcap_set_timeout(handle, 1);

	if (pcap_activate(handle) < 0) {
		log_error("pcap: unable to activate device %s (%s)", dev, pcap_geterr(handle));
		pcap_close(handle);
		return -1;
	}

	if (pcap_setnonblock(handle, 1, errbuf)) {
		log_error("pcap: cannot set to non-blocking mode (%s)", errbuf);
		pcap_close(handle);
		return -1;
	}

	/* TODO direction does not seem to have an effect (we get our own frames every time we poll) */
	if (pcap_setdirection(handle, PCAP_D_IN)) {
		log_warn("pcap: unable to monitor only incoming traffic on device %s (%s)", dev, pcap_geterr(handle));
	}

	if (pcap_datalink(handle) != DLT_IEEE802_11_RADIO) {
		log_error("pcap: device %s does not support radiotap headers", dev);
		pcap_close(handle);
		return -1;
	}

	snprintf(filter_str, sizeof(filter_str), "wlan addr3 %s", ether_ntoa(bssid_filter));
	if (pcap_compile(handle, &filter, filter_str, 1, PCAP_NETMASK_UNKNOWN) == -1) {
		log_error("pcap: could not create filter (%s)", pcap_geterr(handle));
		pcap_close(handle);
		return -1;
	}

	if (pcap_setfilter(handle, &filter) == -1) {
		log_error("pcap: could not set filter (%s)", pcap_geterr(handle));
		pcap_freecode(&filter);
		pcap_close(handle);
		return -1;
	}
	pcap_freecode(&filter);

	if ((fd = pcap_get_selectable_fd(handle)) == -1) {
		log_error("pcap: unable to get fd");
		pcap_close(handle);
		return -1;
	}

	*pcap_handle = handle;

	return fd;
}

static int open_savefile(const char *filename, pcap_t **pcap_handle) {
	char errbuf[PCAP_ERRBUF_SIZE];
	int fd;

	pcap_t *handle = pcap_open_offline(filename, errbuf);
	if (!handle) {
		log_trace("pcap: unable to open savefile (%s)", errbuf);
		return -1;
	}

	if (pcap_datalink(handle) != DLT_IEEE802_11_RADIO) {
		log_error("pcap: savefile does not contain radiotap frames");
		pcap_close(handle);
		return -1;
	}
	if ((fd = pcap_get_selectable_fd(handle)) == -1) {
		log_trace("pcap: unable to get fd");
		pcap_close(handle);
		return -1;
	}

	*pcap_handle = handle;

	return fd;
}

static int open_tun(char *dev, const struct ether_addr *self) {
#ifndef __APPLE__
	struct ifreq ifr = {0};
	int fd = open("/dev/net/tun", O_RDWR | O_NONBLOCK | O_CLOEXEC);
	int socket_fd = -1;
	if (fd < 0)
		return -errno;
	ifr.ifr_flags = IFF_TAP | IFF_NO_PI;
	snprintf(ifr.ifr_name, sizeof(ifr.ifr_name), "%s", dev);
	if (ioctl(fd, TUNSETIFF, &ifr) < 0)
		goto fail;
	strcpy(dev, ifr.ifr_name);
	socket_fd = socket(AF_INET6, SOCK_DGRAM | SOCK_CLOEXEC, 0);
	if (socket_fd < 0)
		goto fail;
	ifr.ifr_hwaddr.sa_family = 1; /* ARPHRD_ETHER */
	memcpy(ifr.ifr_hwaddr.sa_data, self, sizeof(*self));
	if (ioctl(socket_fd, SIOCSIFHWADDR, &ifr) < 0 || ioctl(socket_fd, SIOCGIFFLAGS, &ifr) < 0)
		goto fail;
	ifr.ifr_flags |= IFF_UP | IFF_RUNNING;
	if (ioctl(socket_fd, SIOCSIFFLAGS, &ifr) < 0)
		goto fail;
	ifr.ifr_mtu = 1450;
	if (ioctl(socket_fd, SIOCSIFMTU, &ifr) < 0)
		goto fail;
	close(socket_fd);
	return fd;
fail: {
	int error = errno;
	log_error("tun: setup failed: %s", strerror(error));
	if (socket_fd >= 0)
		close(socket_fd);
	close(fd);
	return -error;
}
#else
	for (int i = 0; i < 16; ++i) {
		char tuntap[IFNAMSIZ];
		sprintf(tuntap, "/dev/tap%d", i);

		int fd = open(tuntap, O_RDWR);
		if (fd > 0) {
			struct ifreq ifr;
			struct in6_aliasreq ifr6;
			int err, s;

			if (fcntl(fd, F_SETFL, O_NONBLOCK) < 0) {
				log_error("fcntl error on %s", tuntap);
				return -errno;
			}

			sprintf(dev, "tap%d", i);

			// Create a socket for ioctl
			s = socket(AF_INET6, SOCK_DGRAM, 0);

			// Set HW address
			strlcpy(ifr.ifr_name, dev, sizeof(ifr.ifr_name));
			ifr.ifr_addr.sa_len = sizeof(struct ether_addr);
			ifr.ifr_addr.sa_family = AF_LINK;
			memcpy(ifr.ifr_addr.sa_data, self, sizeof(struct ether_addr));
			if ((err = ioctl(s, SIOCSIFLLADDR, (caddr_t) &ifr)) < 0) {
				log_error("tun: unable to set HW address %s", ether_ntoa(self));
				close(fd);
				return err;
			}

			/* Set reduced MTU */
			ifr.ifr_mtu = 1450; /* TODO arbitary limit to fit all headers */
			if ((err = ioctl(s, SIOCSIFMTU, (caddr_t) &ifr)) < 0) {
				log_error("tun: unable to set MTU");
				close(fd);
				return err;
			}

			/* Set IPv6 address */
			memset(&ifr6, 0, sizeof(ifr6));
			strlcpy(ifr6.ifra_name, dev, sizeof(ifr6.ifra_name));

			ifr6.ifra_addr.sin6_len = sizeof(ifr6.ifra_addr);
			ifr6.ifra_addr.sin6_family = AF_INET6;
			rfc4291_addr(self, &ifr6.ifra_addr.sin6_addr);

			ifr6.ifra_prefixmask.sin6_len = sizeof(ifr6.ifra_prefixmask);
			ifr6.ifra_prefixmask.sin6_family = AF_INET6;
			memset((void *) &ifr6.ifra_prefixmask.sin6_addr, 0x00, sizeof(ifr6.ifra_prefixmask));
			for (int i = 0; i < 8; i++) /* prefix length: 64 */
				ifr6.ifra_prefixmask.sin6_addr.s6_addr[i] = 0xff;

			if (ioctl(s, SIOCAIFADDR_IN6, (caddr_t) &ifr6) < 0) {
				log_error("tun: unable to set IPv6 address, %s", strerror(errno));
				close(fd);
				return -errno;
			}

			close(s);

			return fd;
		}
	}
	log_error("tun: cannot open available device");
	return -1;
#endif /* __APPLE__ */
}

static int io_state_init_wlan_try_savefile(struct io_state *state) {
	int err;

	state->wlan_fd = open_savefile(state->wlan_ifname, &state->wlan_handle);
	if ((err = state->wlan_fd) < 0)
		return err;
	state->wlan_is_file = 1;
	state->wlan_ifindex = 0;

	/* A savefile has no MAC address to read, so if_ether_addr would stay all
	 * zeroes -- and open_tun() then fails with "unable to set HW address",
	 * because an all-zero address is not assignable. That made replaying a
	 * capture impossible even though the rest of the file path works fine.
	 * Synthesise a locally-administered address instead (bit 1 of the first
	 * octet set, bit 0 clear = unicast). Randomised so that two replays do not
	 * collide if they are ever run side by side. */
	{
		int rnd = open("/dev/urandom", O_RDONLY);
		if (rnd < 0 || read(rnd, &state->if_ether_addr, sizeof(state->if_ether_addr)) !=
		               (ssize_t) sizeof(state->if_ether_addr)) {
			if (rnd >= 0)
				close(rnd);
			log_error("savefile: could not generate a local MAC address");
			pcap_close(state->wlan_handle);
			state->wlan_handle = NULL;
			state->wlan_fd = -1;
			state->wlan_is_file = 0;
			return -1;
		}
		close(rnd);
	}
	state->if_ether_addr.ether_addr_octet[0] = (state->if_ether_addr.ether_addr_octet[0] & 0xfc) | 0x02;

	return 0;
}

static int io_state_init_wlan(struct io_state *state, const char *wlan, const struct ether_addr *bssid_filter) {
	int err;

	strcpy(state->wlan_ifname, wlan);
	state->wlan_is_file = 0;

	if (!io_state_init_wlan_try_savefile(state)) {
		log_info("Using savefile instead of device");
		return 0;
	}

	state->wlan_ifindex = if_nametoindex(state->wlan_ifname);
	if (!state->wlan_ifindex) {
		log_error("No such interface exists %s", state->wlan_ifname);
		return -ENOENT;
	}
	err = netutils_init();
	if (err < 0)
		return err;
	state->netutils_ready = 1;
	/* TODO we might instead open a new monitor interface instead */
	err = link_down(state->wlan_ifindex);
	if (err < 0) {
		log_error("Could not set link down: %s", state->wlan_ifname);
		return err;
	}
	if (!state->wlan_no_monitor_mode) /* if device is already in monitor mode */
		err = set_monitor_mode(state->wlan_ifindex);
	if (err < 0) {
		log_error("Could not put device in monitor mode: %s", state->wlan_ifname);
		return err;
	}
	err = link_up(state->wlan_ifindex);
	if (err < 0) {
		log_error("Could not set link up: %s", state->wlan_ifname);
		return err;
	}
	state->wlan_fd = open_nonblocking_device(state->wlan_ifname, &state->wlan_handle, bssid_filter);
	if (state->wlan_fd < 0) {
		log_error("Could not open device: %s", state->wlan_ifname);
		return state->wlan_fd;
	}
	err = link_ether_addr_get(state->wlan_ifname, &state->if_ether_addr);
	if (err < 0) {
		log_error("Could not get LLC address from %s", state->wlan_ifname);
		return err;
	}
	if (state->addr_override) {
		log_info("AWDL address %s (overrides %s's own)", ether_ntoa(&state->override_addr), state->wlan_ifname);
		state->if_ether_addr = state->override_addr;
	}

	return 0;
}

static int io_state_init_host(struct io_state *state, const char *host) {
	int err;

	if (strlen(host) > 0) {
		strcpy(state->host_ifname, host);
		/* Host interface needs to have same ether_addr, to make active (!) monitor mode work */
		state->host_fd = open_tun(state->host_ifname, &state->if_ether_addr);
		if ((err = state->host_fd) < 0) {
			log_error("Could not open device: %s", state->host_ifname);
			return err;
		}
		state->host_ifindex = if_nametoindex(state->host_ifname);
		if (!state->host_ifindex) {
			log_error("No such interface exists %s", state->host_ifname);
			return -ENOENT;
		}
	} else {
		log_debug("No host device given, start without host device");
	}

	return 0;
}

int io_state_init(struct io_state *state, const char *wlan, const char *host, const struct ether_addr *bssid_filter) {
	state->host_fd = -1;
	state->wlan_fd = -1;
	state->host_ifindex = 0;
	state->wlan_handle = NULL;
	state->netutils_ready = 0;
	if (!wlan || !host || strlen(wlan) >= sizeof(state->wlan_ifname) || strlen(host) >= sizeof(state->host_ifname))
		return -EINVAL;
	int err = io_state_init_wlan(state, wlan, bssid_filter);
	if (err == 0)
		err = io_state_init_host(state, host);
	if (err < 0)
		io_state_free(state);
	return err;
}

void io_state_free(struct io_state *state) {
	if (state->host_fd >= 0)
		close(state->host_fd);
	state->host_fd = -1;
	if (state->wlan_handle)
		pcap_close(state->wlan_handle);
	state->wlan_handle = NULL;
	state->wlan_fd = -1;
	if (state->netutils_ready)
		netutils_cleanup();
	state->netutils_ready = 0;
}

int wlan_send(const struct io_state *state, const uint8_t *buf, int len) {
	if (!state || !state->wlan_handle || !buf || len <= 0 || state->wlan_is_file)
		return -EINVAL;
	errno = 0;
	int sent = pcap_inject(state->wlan_handle, buf, len);
	if (sent < 0) {
		if (errno == EAGAIN || errno == ENOBUFS || errno == EINTR)
			return -EAGAIN;
		log_error("unable to inject packet (%s)", pcap_geterr(state->wlan_handle));
		return -EIO;
	}
	return sent == len ? 0 : -EIO;
}

int host_send(const struct io_state *state, const uint8_t *buf, int len) {
	if (!state || state->host_fd < 0 || !buf || len <= 0)
		return -EINVAL;
	ssize_t sent;
	do {
		sent = write(state->host_fd, buf, len);
	} while (sent < 0 && errno == EINTR);
	if (sent < 0)
		return -errno;
	return sent == len ? 0 : -EIO;
}

int host_recv(const struct io_state *state, uint8_t *buf, int *len) {
	if (!state || state->host_fd < 0 || !buf || !len || *len <= 0)
		return -EINVAL;
	ssize_t received = read(state->host_fd, buf, *len);
	if (received < 0)
		return -errno;
	if (received == 0)
		return -EPIPE;
	*len = (int) received;
	return 0;
}
