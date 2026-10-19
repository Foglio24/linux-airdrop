# Build the customised OWL backend with GNU Make and the bundled test sources.
#
#   make                 builds build/owl
#   make check           runs unit, daemon, CLI and downstream regression tests
#   make sanitize        builds and tests every OWL object under ASan + UBSan
#   make install-backend installs the release binary and its build manifest
#
# Requires a C/C++ toolchain, Python 3, pkg-config and development packages for
# libpcap, libnl-3, libnl-genl-3, libnl-route-3 and libev. libev is linked statically.
# B=... selects an independent build directory; standard compiler/linker flags
# are respected and changes to them automatically rebuild existing objects.

.DEFAULT_GOAL := all
.DELETE_ON_ERROR:

CC       ?= gcc
CXX      ?= g++
PYTHON   ?= python3
PKG_CONFIG ?= pkg-config
B        ?= build
OPT      ?= -O3 -fno-strict-aliasing
WARN     ?= -Wall -Wextra
SANITIZE ?= 0
PKGS      = libpcap libnl-3.0 libnl-genl-3.0 libnl-route-3.0
PKGCFLAGS := $(shell $(PKG_CONFIG) --cflags $(PKGS) 2>/dev/null)
PKGLIBS   := $(shell $(PKG_CONFIG) --libs $(PKGS) 2>/dev/null)
LIBEV    ?= $(firstword $(wildcard /usr/lib/$(shell $(CC) -dumpmachine)/libev.a /usr/lib/libev.a /usr/local/lib/libev.a))
RTFLAGS   = -D_BSD_SOURCE -D_DEFAULT_SOURCE -DRADIOTAP_SUPPORT_OVERRIDES

ifeq ($(SANITIZE),1)
BUILD_OPT = -O1 -g -fno-strict-aliasing
SAN_FLAGS = -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer
# Some inherited unit fixtures do not release their peer tables. Memory access
# and undefined behaviour checks remain enabled; callers can enable leak checks.
ASAN_OPTIONS ?= detect_leaks=0
UBSAN_OPTIONS ?= halt_on_error=1:print_stacktrace=1
export ASAN_OPTIONS UBSAN_OPTIONS
else
BUILD_OPT = $(OPT)
SAN_FLAGS =
endif

OWL_CPPFLAGS = $(CPPFLAGS) -Isrc -Iradiotap $(PKGCFLAGS)
OWL_CFLAGS = $(BUILD_OPT) $(WARN) $(CFLAGS) $(SAN_FLAGS)
OWL_CXXFLAGS = $(BUILD_OPT) $(WARN) $(CXXFLAGS) -std=c++17 -include cstdint $(SAN_FLAGS)
OWL_LDFLAGS = $(LDFLAGS) $(SAN_FLAGS)
DEPFLAGS = -MMD -MP

LIB_SRC = $(addprefix src/,log.c state.c election.c sync.c channel.c schedule.c tx.c rx.c frame.c crc32.c wire.c \
          peers.c version.c hashmap.c siphash24.c circular_buffer.c)
DAEMON_SRC = $(addprefix daemon/,io.c core.c netutils.c owl.c)
LIB_OBJ = $(patsubst %.c,$(B)/%.o,$(LIB_SRC))
DAEMON_OBJ = $(patsubst %.c,$(B)/%.o,$(DAEMON_SRC))
RT_OBJ = $(B)/radiotap/radiotap.o
TEST_SRC = $(wildcard tests/*.cpp)
TEST_OBJ = $(patsubst tests/%.cpp,$(B)/unit-tests/%.o,$(TEST_SRC))
HARNESS_SRC = $(wildcard downstream-tests/owl_*.c)
HARNESS_OBJ = $(patsubst %.c,$(B)/%.o,$(HARNESS_SRC))
HARNESS_BIN = $(patsubst downstream-tests/%.c,$(B)/%,$(HARNESS_SRC))
DAEMON_TEST_OBJ = $(B)/downstream-tests/test_daemon.o
DAEMON_TEST_WRAP = -Wl,--wrap=wlan_send,--wrap=host_send,--wrap=host_recv,--wrap=clock_time_us
GT = $(B)/googletest
GT_TAR = ../sources/owl-modern-source.tar.gz
GT_OBJ = $(B)/gtest-all.o $(B)/gtest_main.o
CONFIG = $(B)/.build-config.json

# Passing metadata through the environment avoids interpolating compiler flags
# into shell quotes or hand-encoding them as JSON.
export OWL_BUILD_CC := $(CC)
export OWL_BUILD_CXX := $(CXX)
export OWL_BUILD_CPPFLAGS := $(OWL_CPPFLAGS)
export OWL_BUILD_CFLAGS := $(OWL_CFLAGS)
export OWL_BUILD_CXXFLAGS := $(OWL_CXXFLAGS)
export OWL_BUILD_RTFLAGS := $(RTFLAGS)
export OWL_BUILD_LDFLAGS := $(OWL_LDFLAGS)
export OWL_BUILD_LDLIBS := $(LDLIBS) -lm
export OWL_BUILD_PKGLIBS := $(PKGLIBS)
export OWL_BUILD_LIBEV := $(LIBEV)
export OWL_BUILD_SANITIZE := $(SANITIZE)

all: $(B)/owl $(B)/owl.build.json

# The helper changes the timestamp only when effective settings change. Every
# object also depends on the build rules, so incremental and clean builds agree.
$(CONFIG): FORCE Makefile tools/build_info.py
	@$(PYTHON) tools/build_info.py config "$@"

$(B)/owl: $(DAEMON_OBJ) $(LIB_OBJ) $(RT_OBJ) $(CONFIG) $(LIBEV)
	@test -n "$(LIBEV)" || { echo "libev.a not found: install libev-dev or set LIBEV=/path/libev.a"; exit 1; }
	$(CC) $(OWL_LDFLAGS) -o $@ $(filter %.o,$^) $(LIBEV) $(PKGLIBS) $(LDLIBS) -lm

$(B)/owl.build.json: $(B)/owl
	@$(PYTHON) tools/build_info.py record "$@"

$(B)/radiotap/%.o: radiotap/%.c $(CONFIG) Makefile
	@mkdir -p $(dir $@)
	$(CC) $(OWL_CPPFLAGS) $(OWL_CFLAGS) $(RTFLAGS) $(DEPFLAGS) -c -o $@ $<

$(B)/%.o: %.c $(CONFIG) Makefile
	@mkdir -p $(dir $@)
	$(CC) $(OWL_CPPFLAGS) $(OWL_CFLAGS) $(DEPFLAGS) -c -o $@ $<

# Unit and daemon test sources get their own dependency files, including OWL
# headers. The old single compile/link command could silently reuse stale tests.
$(B)/unit-tests/%.o: tests/%.cpp $(GT)/.unpacked $(CONFIG) Makefile
	@mkdir -p $(dir $@)
	$(CXX) $(OWL_CPPFLAGS) $(OWL_CXXFLAGS) -I$(GT)/include $(DEPFLAGS) -c -o $@ $<

$(B)/downstream-tests/%.o: downstream-tests/%.cpp $(GT)/.unpacked $(CONFIG) Makefile
	@mkdir -p $(dir $@)
	$(CXX) $(OWL_CPPFLAGS) $(OWL_CXXFLAGS) -Idaemon -I$(GT)/include $(DEPFLAGS) -c -o $@ $<

$(GT)/.unpacked: $(GT_TAR)
	@mkdir -p $(GT)
	tar -xzf $(GT_TAR) -C $(GT) --strip-components=3 owl-modern/googletest/googletest
	@touch $@

$(B)/gtest-all.o: $(GT)/.unpacked $(CONFIG) Makefile
	$(CXX) $(CPPFLAGS) $(OWL_CXXFLAGS) -w -I$(GT)/include -I$(GT) $(DEPFLAGS) -c -o $@ $(GT)/src/gtest-all.cc

$(B)/gtest_main.o: $(GT)/.unpacked $(CONFIG) Makefile
	$(CXX) $(CPPFLAGS) $(OWL_CXXFLAGS) -w -I$(GT)/include -I$(GT) $(DEPFLAGS) -c -o $@ $(GT)/src/gtest_main.cc

$(B)/tests: $(TEST_OBJ) $(LIB_OBJ) $(RT_OBJ) $(GT_OBJ) $(CONFIG)
	$(CXX) $(OWL_LDFLAGS) -o $@ $(filter %.o,$^) $(LDLIBS) -lpthread -lm

test: $(B)/tests
	$(B)/tests

$(B)/daemon-tests: $(DAEMON_TEST_OBJ) $(filter-out %/owl.o,$(DAEMON_OBJ)) $(LIB_OBJ) $(RT_OBJ) $(GT_OBJ) $(CONFIG) $(LIBEV)
	@test -n "$(LIBEV)" || { echo "libev.a not found: install libev-dev or set LIBEV=/path/libev.a"; exit 1; }
	$(CXX) $(OWL_LDFLAGS) $(DAEMON_TEST_WRAP) -o $@ $(filter %.o,$^) $(LIBEV) $(PKGLIBS) $(LDLIBS) -lpthread -lm

daemon-tests: $(B)/daemon-tests
	$(B)/daemon-tests

$(B)/owl_%: $(B)/downstream-tests/owl_%.o $(LIB_OBJ) $(RT_OBJ) $(CONFIG)
	$(CC) $(OWL_LDFLAGS) -o $@ $(filter %.o,$^) $(LDLIBS) -lm

harness: $(HARNESS_BIN)
	@set -e; for harness in $(HARNESS_BIN); do "$$harness"; done
	@echo "downstream harnesses passed"

cli-tests: $(B)/owl
	OWL_BINARY="$(abspath $(B)/owl)" $(PYTHON) -B -m unittest discover -s downstream-tests -p test_owl_cli.py

check: all test daemon-tests cli-tests harness

# A nested directory isolates sanitized objects from release objects even when
# callers select B themselves. The recursive build instruments the entire OWL
# codebase, including daemon objects and every test translation unit.
sanitize:
	$(MAKE) B="$(B)/sanitize" SANITIZE=1 check

# Install only the selected build; the manifest records its actual flags and
# source snapshot, and uses a JSON encoder for paths and compiler version text.
install-backend: $(B)/owl $(B)/owl.build.json
	install -m 755 $(B)/owl ../owl-nexa
	@$(PYTHON) tools/build_info.py manifest "$(B)/owl.build.json" ../owl-nexa ../owl-nexa.manifest.json
	@cat ../owl-nexa.manifest.json

clean:
	rm -rf -- "$(B)"

-include $(LIB_OBJ:.o=.d) $(DAEMON_OBJ:.o=.d) $(RT_OBJ:.o=.d) $(TEST_OBJ:.o=.d) \
         $(HARNESS_OBJ:.o=.d) $(DAEMON_TEST_OBJ:.o=.d) $(GT_OBJ:.o=.d)

# Keep intermediate harness objects and their header dependency information.
.SECONDARY: $(HARNESS_OBJ)
.PHONY: all test daemon-tests cli-tests harness check sanitize clean install-backend FORCE
