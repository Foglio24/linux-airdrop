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

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <syslog.h>
#include <unistd.h>
#include <ev.h>

#include "log.h"
#include "core.h"
#include "tx.h"

#define DEFAULT_AWDL_DEVICE "awdl0"
#define FAILED_DUMP "failed.pcap"

struct owl_config {
	int daemon, dump, log_level, filter_rssi, fixed_radio, fixed_ack;
	int metric_first, no_monitor_mode, widen_max, start_guard_tu, end_guard_tu;
	int rssi_threshold, rssi_mode, have_addr_override;
	struct awdl_chan chan;
	enum awdl_chanseq_strategy strategy;
	struct ether_addr addr_override;
	struct ieee80211_tx_rate data_tx_rate;
	char wlan[PATH_MAX];
	char host[IFNAMSIZ];
};

static void print_usage(FILE *out, const char *program) {
	fprintf(out,
	        "Usage: %s -i interface-or-capture [options]\n"
	        "  --help       Show this help and exit\n"
	        "  -i source    Wireless interface or pcap capture\n"
	        "  -h name      Host interface (default awdl0; empty disables it)\n"
	        "  -c channel   Channel: 6, 36, 44, or 149 (default 6)\n"
	        "  -m address   Override AWDL MAC address\n"
	        "  -D           Run in the background\n"
	        "  -d           Dump rejected frames to failed.pcap\n"
	        "  -v           Increase verbosity (may be repeated)\n"
	        "  -N           Interface is already in monitor mode\n"
	        "  -F           Keep the radio on the selected channel\n"
	        "  -A           Enable hardware ACK/retries\n"
	        "  -E           Elect the sync master by metric, then counter\n"
	        "  -S strategy  verbatim, widen, intersect, rotate, or pin\n"
	        "  -W count     Maximum widened slots: 0..%d (default 4)\n"
	        "  -G start[,end]  Unicast guards in TU: 0..16 (default 3,3)\n"
	        "  -R rate      6, 9, 12, 18, 24, 36, 48, 54; mcs0..mcs15[-sgi]\n"
	        "  -r threshold RSSI threshold in dBm: -100..-30\n"
	        "  -I           Filter RSSI per frame instead of running average\n"
	        "  -f           Disable RSSI filtering\n",
	        program, AWDL_CHANSEQ_LENGTH);
}

/* Parse a single decimal component without accepting whitespace, empty input,
 * overflow, or a numeric prefix followed by ignored junk. */
static int parse_number(const char *text, int min, int max, int *value, const char **tail) {
	const char *digits = text;
	char *end;
	long parsed;
	if (*digits == '+' || *digits == '-')
		digits++;
	if (!isdigit((unsigned char) *digits))
		return -1;
	errno = 0;
	parsed = strtol(text, &end, 10);
	if (errno == ERANGE || parsed < min || parsed > max || (!tail && *end))
		return -1;
	*value = (int) parsed;
	if (tail)
		*tail = end;
	return 0;
}

static int parse_guards(const char *text, int *start, int *end) {
	const char *tail;
	if (parse_number(text, 0, 16, start, &tail) < 0)
		return -1;
	if (!*tail) {
		*end = *start;
		return 0;
	}
	return *tail == ',' ? parse_number(tail + 1, 0, 16, end, NULL) : -1;
}

static int parse_address(const char *text, struct ether_addr *address) {
	/* ether_aton accepts trailing text on some systems. Validate every octet
	 * first, retaining the one- or two-digit notation it supports. */
	const char *cursor = text;
	for (size_t i = 0; i < 6; i++) {
		int digits = 0;
		while (isxdigit((unsigned char) *cursor)) {
			if (++digits > 2)
				return -1;
			cursor++;
		}
		if (!digits || (i < 5 ? *cursor != ':' : *cursor != '\0'))
			return -1;
		if (i < 5)
			cursor++;
	}
	return ether_aton_r(text, address) ? 0 : -1;
}

static int parse_rate(const char *text, struct ieee80211_tx_rate *rate) {
	const char *digits = !strncmp(text, "mcs", 3) ? text + 3 : text;
	if (!isdigit((unsigned char) *digits))
		return -1;
	while (isdigit((unsigned char) *digits))
		digits++;
	if (*digits && (strncmp(text, "mcs", 3) || strcmp(digits, "-sgi")))
		return -1;
	return ieee80211_parse_tx_rate(text, rate);
}

static int copy_option(char *destination, size_t capacity, const char *value, int option) {
	if (strlen(value) >= capacity) {
		fprintf(stderr, "-%c takes at most %zu bytes\n", option, capacity - 1);
		return -1;
	}
	strcpy(destination, value);
	return 0;
}

/* All validation precedes daemonization and initialization. A rejected command
 * must neither change a network interface nor report success from a fork. */
static int parse_options(int argc, char **argv, struct owl_config *config) {
	static const struct option options[] = {
		{ "help", no_argument, NULL, 256 },
		{ NULL, 0, NULL, 0 }
	};
	int c, channel = 6;
	*config = (struct owl_config) {
		.log_level = LOG_INFO, .filter_rssi = 1,
		.strategy = AWDL_CHANSEQ_VERBATIM, .widen_max = 4,
		.start_guard_tu = -1, .end_guard_tu = -1,
		.rssi_threshold = RSSI_THRESHOLD_DEFAULT, .rssi_mode = -1,
		.data_tx_rate = { .legacy = 12, .mcs = -1, .sgi = 0 },
		.host = DEFAULT_AWDL_DEVICE,
	};
	opterr = 0;
	while ((c = getopt_long(argc, argv, ":Dc:dvi:h:fNS:W:R:m:FEAG:r:I", options, NULL)) != -1) {
		switch (c) {
		case 256:
			print_usage(stdout, argv[0]);
			return 1;
		case 'm':
			if (parse_address(optarg, &config->addr_override) < 0) {
				fprintf(stderr, "-m takes a MAC address, not '%s'\n", optarg);
				return -1;
			}
			config->have_addr_override = 1;
			break;
		case 'R':
			if (parse_rate(optarg, &config->data_tx_rate) < 0) {
				fprintf(stderr, "-R takes a legacy rate (6, 9, 12, 18, 24, 36, 48, 54), "
				        "mcs0..mcs15 or mcsN-sgi, not '%s'\n", optarg);
				return -1;
			}
			break;
		case 'S':
			if (!strcmp(optarg, "pin")) config->strategy = AWDL_CHANSEQ_PIN;
			else if (!strcmp(optarg, "rotate")) config->strategy = AWDL_CHANSEQ_ROTATE;
			else if (!strcmp(optarg, "verbatim")) config->strategy = AWDL_CHANSEQ_VERBATIM;
			else if (!strcmp(optarg, "widen")) config->strategy = AWDL_CHANSEQ_WIDEN;
			else if (!strcmp(optarg, "intersect")) config->strategy = AWDL_CHANSEQ_INTERSECT;
			else {
				fprintf(stderr, "Unknown channel sequence strategy '%s' "
				        "(use verbatim, widen, intersect, rotate, or pin)\n", optarg);
				return -1;
			}
			break;
		case 'G':
			if (parse_guards(optarg, &config->start_guard_tu, &config->end_guard_tu) < 0) {
				fprintf(stderr, "-G takes start[,end] in TU, 0..16 (e.g. -G 1 or -G 2,1)\n");
				return -1;
			}
			break;
		case 'r':
			if (parse_number(optarg, -100, -30, &config->rssi_threshold, NULL) < 0) {
				fprintf(stderr, "-r takes a threshold in dBm, -100..-30\n");
				return -1;
			}
			break;
		case 'W':
			if (parse_number(optarg, 0, AWDL_CHANSEQ_LENGTH, &config->widen_max, NULL) < 0) {
				fprintf(stderr, "-W takes 0..%d\n", AWDL_CHANSEQ_LENGTH);
				return -1;
			}
			break;
		case 'c':
			if (parse_number(optarg, 0, 255, &channel, NULL) < 0) {
				fprintf(stderr, "-c takes a channel: 6, 36, 44, or 149\n");
				return -1;
			}
			break;
		case 'i':
			if (copy_option(config->wlan, sizeof(config->wlan), optarg, c) < 0) return -1;
			break;
		case 'h':
			if (copy_option(config->host, sizeof(config->host), optarg, c) < 0) return -1;
			break;
		case 'v':
			if (config->log_level < LOG_TRACE) config->log_level++;
			break;
		case 'D': config->daemon = 1; break;
		case 'd': config->dump = 1; break;
		case 'f': config->filter_rssi = 0; break;
		case 'I': config->rssi_mode = AWDL_RSSI_INSTANT; break;
		case 'E': config->metric_first = 1; break;
		case 'A': config->fixed_ack = 1; break;
		case 'F': config->fixed_radio = 1; break;
		case 'N': config->no_monitor_mode = 1; break;
		case ':':
			fprintf(stderr, "Option -%c needs an argument\n", optopt);
			return -1;
		default:
			fprintf(stderr, "Unknown option '%s'; use --help\n", argv[optind - 1]);
			return -1;
		}
	}
	if (optind != argc) {
		fprintf(stderr, "Unexpected argument '%s'; use --help\n", argv[optind]);
		return -1;
	}
	/* Keep this supported set in step with the bridge's channel choices. */
	switch (channel) {
	case 6: config->chan = CHAN_OPCLASS_6; break;
	case 36: config->chan = CHAN_OPCLASS_36; break;
	case 44: config->chan = CHAN_OPCLASS_44; break;
	case 149: config->chan = CHAN_OPCLASS_149; break;
	default:
		fprintf(stderr, "Unsupported channel %d (use 6, 36, 44, or 149)\n", channel);
		return -1;
	}
	if (!*config->wlan) {
		fprintf(stderr, "No interface specified; use -i interface-or-capture\n");
		return -1;
	}
	return 0;
}

/* The original process waits for startup, so a background initialization
 * failure has the same exit status as a foreground failure. */
static int daemonize(void) {
	int status_pipe[2];
	pid_t pid;
	if (pipe(status_pipe) < 0)
		return -1;
	/* Keep the handshake independent of whether the caller closed stdio. */
	for (int i = 0; i < 2; i++) {
		if (status_pipe[i] <= STDERR_FILENO) {
			int descriptor = fcntl(status_pipe[i], F_DUPFD_CLOEXEC, STDERR_FILENO + 1);
			if (descriptor < 0) {
				close(status_pipe[0]);
				close(status_pipe[1]);
				return -1;
			}
			close(status_pipe[i]);
			status_pipe[i] = descriptor;
		}
	}
	fflush(NULL);
	pid = fork();
	if (pid < 0) {
		close(status_pipe[0]);
		close(status_pipe[1]);
		return -1;
	}
	if (pid > 0) {
		unsigned char status = EXIT_FAILURE;
		ssize_t received;
		close(status_pipe[1]);
		do {
			received = read(status_pipe[0], &status, sizeof(status));
		} while (received < 0 && errno == EINTR);
		close(status_pipe[0]);
		while (waitpid(pid, NULL, 0) < 0 && errno == EINTR) {}
		if (received != (ssize_t) sizeof(status) || status != EXIT_SUCCESS) {
			fprintf(stderr, "OWL background initialization failed\n");
			_exit(EXIT_FAILURE);
		}
		_exit(EXIT_SUCCESS);
	}
	close(status_pipe[0]);
	if (setsid() < 0)
		_exit(EXIT_FAILURE);
	signal(SIGCHLD, SIG_IGN);
	signal(SIGHUP, SIG_IGN);
	pid = fork();
	if (pid < 0)
		_exit(EXIT_FAILURE);
	if (pid > 0)
		_exit(EXIT_SUCCESS);
	umask(0);
	long max_fd = sysconf(_SC_OPEN_MAX);
	if (max_fd < 0)
		max_fd = 1024;
	for (long fd = max_fd - 1; fd >= 0; fd--)
		if (fd != status_pipe[1])
			close((int) fd);
	int null_fd = open("/dev/null", O_RDWR);
	if (null_fd < 0)
		_exit(EXIT_FAILURE);
	for (int fd = STDIN_FILENO; fd <= STDERR_FILENO; fd++)
		if (dup2(null_fd, fd) < 0)
			_exit(EXIT_FAILURE);
	if (null_fd > STDERR_FILENO)
		close(null_fd);
	openlog("owl", LOG_PID, LOG_DAEMON);
	return status_pipe[1];
}

static void notify_startup(int fd, unsigned char status) {
	if (fd < 0)
		return;
	ssize_t written;
	do {
		written = write(fd, &status, sizeof(status));
	} while (written < 0 && errno == EINTR);
	close(fd);
}

static void apply_config(struct daemon_state *state, const struct owl_config *config) {
	state->awdl_state.filter_rssi = config->filter_rssi;
	state->awdl_state.rssi_threshold = (signed char) config->rssi_threshold;
	if (config->rssi_mode >= 0)
		state->awdl_state.rssi_mode = (enum awdl_rssi_mode) config->rssi_mode;
	if (config->start_guard_tu >= 0) {
		state->awdl_state.tx.start_guard_tu = config->start_guard_tu;
		state->awdl_state.tx.end_guard_tu = config->end_guard_tu;
	}
	state->awdl_state.channel.strategy = config->strategy;
	state->awdl_state.channel.widen_max = config->widen_max;
	state->awdl_state.channel.fixed_radio = config->fixed_radio;
	state->awdl_state.channel.fixed_ack = config->fixed_ack;
	state->awdl_state.election.metric_first = config->metric_first;
	log_info("unicast guard: start %d TU, end %d TU", state->awdl_state.tx.start_guard_tu,
	         state->awdl_state.tx.end_guard_tu);
	if (config->filter_rssi)
		log_info("RSSI filter: %s, threshold %d dBm, grace %d dB",
		         state->awdl_state.rssi_mode == AWDL_RSSI_AVERAGE ? "running average" : "per frame",
		         state->awdl_state.rssi_threshold, state->awdl_state.rssi_grace);
	else
		log_info("RSSI filter: off");
	if (config->fixed_ack)
		log_info("fixed radio experiment: hardware ACK/retries enabled");
	if (config->metric_first)
		log_info("election: by metric, then counter");
	if (config->fixed_radio)
		log_info("fixed radio: multicast in any slot, unicast whenever the peer is on our channel");
	log_info("channel sequence strategy: %s",
	         config->strategy == AWDL_CHANSEQ_PIN ? "pin" :
	         config->strategy == AWDL_CHANSEQ_ROTATE ? "rotate" :
	         config->strategy == AWDL_CHANSEQ_WIDEN ? "widen" :
	         config->strategy == AWDL_CHANSEQ_INTERSECT ? "intersect" : "verbatim");
	if (config->strategy == AWDL_CHANSEQ_WIDEN)
		log_info("widening by at most %d slot(s)", config->widen_max);
	char rate[64];
	ieee80211_format_tx_rate(rate, sizeof(rate), &config->data_tx_rate);
	log_info("data frame TX rate: %s", rate);
	if (state->io.wlan_ifindex)
		log_info("WLAN device: %s (addr %s)", state->io.wlan_ifname, ether_ntoa(&state->io.if_ether_addr));
	if (state->io.host_ifindex)
		log_info("Host device: %s", state->io.host_ifname);
}

static void stop_loop(struct ev_loop *loop, ev_signal *watcher, int events) {
	(void) watcher;
	(void) events;
	ev_break(loop, EVBREAK_ALL);
}

static void print_banner(void) {
	printf("              .oOXWMMMMWXOx:\n"
	       "         .oOOOx:'''''''''''':OOOx:\n"
	       "      oXOo'      ........      ':OXx.\n"
	       "           .oOOO''''''''''OOOo.\n"
	       "        oXOo'                'oOO:\n"
	       "             :oOOOOXXXXOOOOo:.\n"
	       "          oXO:'            ':OXo\n"
	       "              .:xOXXXXXXOx:.\n"
	       "          .xXMMMMMMMMMMMMMMMMXx.\n"
	       "  'XWWWWWWMMMMMMMMMMMMMMMMMMMMMMWWWWWWX'\n"
	       "    oWMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMWo\n"
	       "     OMMMMMMWWMMMMMMMMMMMMMMWWWMMMMMO\n"
	       "    OMMWx'      'xWMMMMWx'      'oXMMO\n"
	       "   :MW:            oMMx            'WM:\n"
	       "   XM'    .xOOo.    :o     .xOOo.    WX\n"
	       "   WX    :MMMMMX          :MMMMMX    xW\n"
	       "   XW    'WMMMMX   .xx.   'WMMMWX    XX\n"
	       "   'Wx    'xWMx'   OMMO    'xWMx'   xM'\n"
	       "    'XX:           'XX'           :XX'\n"
	       "      'xXOx:..................:xXWx'\n"
	       "         'xXMMMMMMMMMMMMMMMMMMWO'\n"
	       "\n"
	       "            Open Wireless Link\n"
	       "\n"
	       "            https://owlink.org\n"
	       "\n");

}

int main(int argc, char *argv[]) {
	struct owl_config config;
	struct daemon_state state = {0};
	struct ev_loop *loop;
	ev_signal terminate, interrupt;
	int startup_fd = -1;
	int parsed = parse_options(argc, argv, &config);
	if (parsed != 0)
		return parsed > 0 ? EXIT_SUCCESS : EXIT_FAILURE;

	log_set_level(config.log_level);
	print_banner();
	if (config.daemon) {
		startup_fd = daemonize();
		if (startup_fd < 0) {
			fprintf(stderr, "Could not start background process: %s\n", strerror(errno));
			return EXIT_FAILURE;
		}
	}
	state.io.wlan_no_monitor_mode = config.no_monitor_mode;
	state.io.addr_override = config.have_addr_override;
	state.io.override_addr = config.addr_override;
	awdl_data_tx_rate = config.data_tx_rate;
	if (awdl_init(&state, config.wlan, config.host, config.chan, config.dump ? FAILED_DUMP : NULL) < 0) {
		log_error("could not initialize core");
		if (config.daemon)
			syslog(LOG_ERR, "could not initialize core");
		notify_startup(startup_fd, EXIT_FAILURE);
		return EXIT_FAILURE;
	}
	apply_config(&state, &config);
	loop = EV_DEFAULT;
	if (!loop) {
		log_error("could not initialize event loop");
		awdl_free(&state);
		notify_startup(startup_fd, EXIT_FAILURE);
		return EXIT_FAILURE;
	}
	/* Open relative capture paths before leaving the caller's directory. */
	if (config.daemon && chdir("/") < 0) {
		syslog(LOG_ERR, "could not change working directory: %s", strerror(errno));
		awdl_free(&state);
		ev_loop_destroy(loop);
		notify_startup(startup_fd, EXIT_FAILURE);
		return EXIT_FAILURE;
	}
	awdl_schedule(loop, &state);
	ev_signal_init(&terminate, stop_loop, SIGTERM);
	ev_signal_start(loop, &terminate);
	ev_signal_init(&interrupt, stop_loop, SIGINT);
	ev_signal_start(loop, &interrupt);
	notify_startup(startup_fd, EXIT_SUCCESS);
	ev_run(loop, 0);
	int exit_status = state.io_failed ? EXIT_FAILURE : EXIT_SUCCESS;
	ev_signal_stop(loop, &terminate);
	ev_signal_stop(loop, &interrupt);
	awdl_free(&state);
	ev_loop_destroy(loop);
	if (config.daemon)
		closelog();
	return exit_status;
}
